"""AutoScientist peptide-design orchestrator (Gao, Fang & Zitnik 2026, arXiv:2605.28655).

A self-organizing, hypothesis-driven alternative to the genetic-algorithm designer. Instead of
random mutation/crossover, an LLM team (Gemini, the same key/model the reports use) drives the
search through a shared experimental state:

  • Discussion / self-organization — propose K research DIRECTIONS (axes), e.g. "add an aromatic
    cluster for π-stacking", "introduce a salt bridge to the acidic pocket". (Alg. 2)
  • Analyst cycle — for each direction, propose candidate sequences from accumulated evidence
    (the experiment log + champion + dead-ends), critiquing/filtering weak ideas BEFORE compute,
    ranked by observed effect size. (Alg. 4)
  • Experiment cycle — evaluate proposals with the EXISTING docking (+ optional MD/MM-GBSA)
    machinery; promote the champion only past a noise-aware gate (re-evaluate borderline gains
    before promotion). (Alg. 3)
  • Stagnation → reorganize — if the champion does not improve for a window of rounds, reopen
    discussion: retire dead-end directions and form new ones. (Alg. 1/4 stagnation trigger)

Coordination is through a shared state (champion p*, experiment log L, research forum F, dead-end
registry) rather than a central planner. The run emits the SAME DesignResult shape as the GA
(generations = per-round best-so-far, candidates = every evaluated peptide) so the existing
persistence, leaderboard, convergence figure, and report all work unchanged, plus an
``autoscientist`` block (directions, forum, dead-ends, champion recipe) for the research report.

Every LLM call degrades gracefully (Gemini down → fall back to guided mutation of the champion),
and a single bad candidate never aborts the run.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import docking, md_eval

_AA = "ACDEFGHIKLMNPQRSTVWY"
_AA_SET = set(_AA)


class AutoScientistCancelled(Exception):
    """Raised to abort an AutoScientist run when the backend has cancelled it."""


# ───────────────────────────── sequence helpers ─────────────────────────────

def _valid(seq: str, length: int) -> bool:
    seq = (seq or "").strip().upper()
    return len(seq) == length and all(c in _AA_SET for c in seq)


def _mutate(seq: str, rng_index: int, n_mut: int = 1) -> str:
    """Deterministic guided mutation (fallback when the LLM is unavailable): substitute n_mut
    positions, cycling residues/positions by rng_index so successive fallbacks differ."""
    chars = list(seq)
    L = len(chars)
    for m in range(max(1, n_mut)):
        pos = (rng_index * 7 + m * 13) % L
        cur = chars[pos]
        chars[pos] = _AA[(_AA.index(cur) + 1 + (rng_index + m)) % len(_AA)] if cur in _AA_SET else "A"
    return "".join(chars)


# ───────────────────────────── Gemini agents ─────────────────────────────

def _safe_generate_json(prompt: str, settings, max_output_tokens: int) -> Optional[Dict[str, Any]]:
    """Call Gemini and return the parsed JSON dict, or None on ANY failure (import error, network,
    bad output). Guarantees the agent helpers degrade to their deterministic fallbacks instead of
    aborting the run."""
    try:
        from mdworker.report import gemini  # lazy: report package owns the client
        return gemini.generate_json(prompt, settings=settings, max_output_tokens=max_output_tokens)
    except Exception:  # noqa: BLE001 — LLM is best-effort; never propagate
        return None


def _log_summary(log: List[Dict[str, Any]], champion: Optional[Dict[str, Any]], limit: int = 24) -> Dict[str, Any]:
    """Compact view of the experiment log for the LLM (best-first, capped)."""
    ranked = sorted([r for r in log if r.get("fitness") is not None],
                    key=lambda r: r["fitness"], reverse=True)[:limit]
    return {
        "champion": ({"sequence": champion["sequence"], "fitness": round(champion["fitness"], 3),
                      "docking_score": champion.get("docking_score"),
                      "md_dg": champion.get("md_dg")} if champion else None),
        "evaluated": [{"sequence": r["sequence"], "fitness": round(r["fitness"], 3),
                       "docking_score": r.get("docking_score"), "md_dg": r.get("md_dg"),
                       "direction": r.get("direction")} for r in ranked],
        "n_evaluated": len(log),
    }


def _discuss_directions(settings, *, task: str, peptide_length: int, seeds: List[str],
                        state: Dict[str, Any], k: int) -> List[Dict[str, str]]:
    prompt = (
        "You are a team of peptide-design scientists self-organizing a binder-optimization "
        "campaign (AutoScientists protocol). Propose research DIRECTIONS to explore — distinct, "
        "mechanistic hypotheses for improving binding of a peptide to the target.\n\n"
        f"TASK: {task}\nPeptide length (fixed): {peptide_length}\n"
        f"Seed sequences: {seeds}\n"
        f"Current evidence: {json.dumps(_log_summary(state['log'], state['champion']), default=str)}\n\n"
        f"Return a JSON object: {{\"directions\": [{{\"axis\": short label, \"hypothesis\": one "
        f"sentence mechanism}}]}} with EXACTLY {k} diverse, non-overlapping directions grounded in "
        "peptide–ligand binding chemistry (hydrophobic packing, aromatic/π interactions, "
        "charge complementarity/salt bridges, H-bond donors/acceptors, helicity/turn motifs, etc.)."
    )
    out = _safe_generate_json(prompt, settings, 4096)
    dirs = (out or {}).get("directions") if isinstance(out, dict) else None
    if not dirs:
        dirs = _DEFAULT_DIRECTIONS[:k]
    return [{"axis": str(d.get("axis", f"direction-{i+1}"))[:60],
             "hypothesis": str(d.get("hypothesis", ""))[:240]} for i, d in enumerate(dirs[:k])]


def _propose_candidates(settings, *, task: str, peptide_length: int, directions: List[Dict[str, str]],
                        state: Dict[str, Any], n_total: int) -> List[Dict[str, str]]:
    """Analyst cycle: propose candidate sequences across the directions, self-critiquing weak ideas
    before they cost compute (Alg. 4). Returns [{sequence, axis, rationale}]."""
    prompt = (
        "You are analyst agents in an AutoScientists peptide-design run. Using ONLY the evidence "
        "below, propose the most PROMISING next candidate peptides to evaluate — critique and drop "
        "weak ideas yourself before they cost compute. Spread proposals across the research "
        "directions, prioritizing directions with the best observed effect so far, and AVOID "
        "sequences already evaluated or listed as dead-ends.\n\n"
        f"TASK: {task}\nPeptide length (MUST be exactly {peptide_length}; use the 20 standard "
        "amino acids only).\n"
        f"Research directions: {json.dumps(directions)}\n"
        f"Evidence: {json.dumps(_log_summary(state['log'], state['champion']), default=str)}\n"
        f"Dead-end directions (do not revisit): {json.dumps(state['dead_ends'][-12:])}\n\n"
        f"Return JSON: {{\"proposals\": [{{\"sequence\": L={peptide_length} string, \"axis\": one of "
        "the direction axes, \"rationale\": short why}}]}} with up to "
        f"{n_total} candidates, each a concrete, plausible improvement (not random)."
    )
    out = _safe_generate_json(prompt, settings, 6144)
    props = (out or {}).get("proposals") if isinstance(out, dict) else None
    result: List[Dict[str, str]] = []
    if props:
        for p in props:
            seq = str(p.get("sequence", "")).strip().upper()
            if _valid(seq, peptide_length):
                result.append({"sequence": seq, "axis": str(p.get("axis", ""))[:60],
                               "rationale": str(p.get("rationale", ""))[:240]})
    return result


def _reorganize(settings, *, task: str, peptide_length: int, directions: List[Dict[str, str]],
                state: Dict[str, Any], k: int) -> Tuple[List[Dict[str, str]], List[str]]:
    """Self-reorganize after stagnation (Alg. 2 mid-run reformation): retire exhausted directions,
    form new ones. Returns (new_directions, retired_axes)."""
    prompt = (
        "An AutoScientists peptide-design run has STAGNATED (no champion improvement recently). "
        "Reorganize the research directions: retire exhausted/unproductive axes and propose new "
        "mechanistic directions to escape the local optimum.\n\n"
        f"TASK: {task}\nPeptide length: {peptide_length}\n"
        f"Current directions: {json.dumps(directions)}\n"
        f"Evidence: {json.dumps(_log_summary(state['log'], state['champion']), default=str)}\n\n"
        f"Return JSON: {{\"retire\": [axis labels to drop], \"directions\": [{{\"axis\", "
        f"\"hypothesis\"}}]}} with EXACTLY {k} directions for the next phase (may keep productive "
        "ones, must add at least one genuinely new mechanism)."
    )
    out = _safe_generate_json(prompt, settings, 4096)
    if not isinstance(out, dict) or not out.get("directions"):
        # Fallback: keep directions, drop the least-productive half by appending fresh defaults.
        fresh = [d for d in _DEFAULT_DIRECTIONS if d["axis"] not in {x["axis"] for x in directions}]
        return ((directions[: max(1, k // 2)] + fresh)[:k], [])
    retired = [str(x)[:60] for x in (out.get("retire") or [])]
    new = [{"axis": str(d.get("axis", f"direction-{i+1}"))[:60],
            "hypothesis": str(d.get("hypothesis", ""))[:240]}
           for i, d in enumerate(out["directions"][:k])]
    return new, retired


_DEFAULT_DIRECTIONS: List[Dict[str, str]] = [
    {"axis": "hydrophobic-core", "hypothesis": "Strengthen hydrophobic packing against the ligand."},
    {"axis": "aromatic-stacking", "hypothesis": "Introduce aromatic residues for π-stacking."},
    {"axis": "charge-complementarity", "hypothesis": "Match charges to the ligand's polar groups."},
    {"axis": "hbond-network", "hypothesis": "Add H-bond donors/acceptors at the interface."},
    {"axis": "turn-motif", "hypothesis": "Promote a turn/helix that presents side chains to the ligand."},
    {"axis": "terminal-anchoring", "hypothesis": "Anchor the termini to deepen the binding pose."},
]


# ───────────────────────────── orchestrator ─────────────────────────────

def run_autoscientist(design_id: str, config: Dict[str, Any], reporter, settings: Dict[str, Any]) -> Dict[str, Any]:
    """Run one AutoScientist peptide-design campaign. Returns the serialized DesignResult dict
    (GA-compatible shape) with an extra ``autoscientist`` block for the research report."""
    def log(msg: str, level: str = "info") -> None:
        reporter.log(design_id, level, "autoscientist", msg)

    reporter.set_status(design_id, "preparing")
    storage_root = Path(settings.get("STORAGE_ROOT", "."))
    workdir = storage_root / "design" / design_id
    workdir.mkdir(parents=True, exist_ok=True)

    gpu_id = reporter.request_gpu(design_id)
    log(f"AutoScientist GPU: {gpu_id if gpu_id is not None else 'none (mock MD / CPU docking)'}.")

    seeds = [s.strip().upper() for s in config["initial_sequences"] if s.strip()]
    peptide_length = len(seeds[0]) if seeds else int(config.get("peptide_length", 7))
    task = (f"Design a {peptide_length}-residue peptide that binds the target compound "
            f"'{config.get('compound_name', 'compound')}' with the most favorable binding energy "
            f"(lower docking score / MM-GBSA ΔG is better).")

    # Budget knobs (reuse DesignJob columns with AutoScientist semantics).
    max_rounds = max(1, int(config.get("num_generations", 5)))            # discussion→execution cycles
    per_round = max(1, int(config.get("population_size", 8)))             # candidates proposed per round
    k_directions = max(1, min(8, int(config.get("dock_oversample", 4))))  # research directions (teams)
    stagnation_window = 2                                                 # rounds w/o improvement → reorganize
    md_length_ns = float(config.get("md_length_ns", 10.0))
    n_replicas = max(1, min(5, int(config.get("n_replicas", 1) or 1)))
    exhaustiveness = int(config.get("exhaustiveness", 16))
    eval_mode = str(config.get("eval_mode", "hybrid"))                    # hybrid: dock all, MD promising; md_only: MD all
    md_engine = str(settings.get("MD_ENGINE", "mock")).lower()
    if md_engine == "auto":
        md_engine = "gromacs" if md_eval.gromacs_available() else "mock"
    dock_engine = docking.resolve_engine(str(settings.get("DOCK_ENGINE", "vina")))
    log(f"Strategy: AutoScientists · {max_rounds} rounds × ≤{per_round} candidates · "
        f"{k_directions} research directions · eval {eval_mode} · dock {dock_engine} · MD {md_engine}.")

    try:
        log(f"Preparing target compound ligand from {config['compound_file']}.")
        ligand_pdbqt = docking.prepare_ligand(config["compound_file"], workdir / "ligand.pdbqt")
    except Exception as exc:  # noqa: BLE001
        reporter.set_status(design_id, "failed", error_message=f"Ligand prep failed: {exc}")
        if gpu_id is not None:
            reporter.release_gpu(design_id)
        raise

    # ── shared state ──
    state: Dict[str, Any] = {
        "champion": None,                # {sequence, fitness, docking_score, md_dg, direction}
        "log": [],                       # every evaluated candidate (dicts)
        "evaluated": set(),              # sequences already evaluated (dedup)
        "dead_ends": [],                 # retired direction axes
        "directions": [],               # current roster
        "forum": [],                    # narrative posts (discussion/reorg/promotions)
    }
    dock_cache: Dict[str, docking.DockResult] = {}
    records: List[Dict[str, Any]] = []   # per-round best-so-far (GA "generations" shape)
    rounds_since_improve = 0

    def evaluate(seq: str, direction: str, rnd: int, *, refine: bool) -> Optional[Dict[str, Any]]:
        """Dock (+ optional MD) one candidate; return its record or None on failure."""
        gen_dir = workdir / f"round_{rnd:02d}"
        try:
            res = dock_cache.get(seq) or docking.dock_peptide_compound(
                seq, ligand_pdbqt, gen_dir, engine=dock_engine,
                exhaustiveness=exhaustiveness, n_poses=5)
        except Exception as exc:  # noqa: BLE001 — one bad candidate must not kill the run
            log(f"Docking failed for {seq}: {exc}", level="warning")
            return None
        if res is None or res.score is None:
            return None
        dock_cache[seq] = res
        rec: Dict[str, Any] = {"sequence": seq, "generation": rnd, "direction": direction,
                               "docking_score": round(float(res.score), 3), "md_dg": None,
                               "refined": False}
        if refine:
            try:
                agg = md_eval.evaluate_replicas(
                    seq, res.score, n_replicas=n_replicas, engine=md_engine,
                    workdir=workdir / "md" / seq, peptide_pdb=res.peptide_pdb,
                    pose_pdbqt=res.pose_pdbqt, gpu_id=gpu_id, md_length_ns=md_length_ns,
                    settings={**settings, "compound_file": config["compound_file"]},
                    log=lambda m: log(m))
                rec["md_dg"] = agg["dg"]
                rec["sem"] = agg.get("sem", 0.0)
                rec["refined"] = True
            except Exception as exc:  # noqa: BLE001
                log(f"MD evaluation failed for {seq}: {exc}", level="warning")
        # Fitness = −energy (higher is better); prefer the MD ΔG when refined, else docking score.
        rec["fitness"] = round(-(rec["md_dg"] if rec["md_dg"] is not None else rec["docking_score"]), 3)
        return rec

    def commit(rec: Dict[str, Any]) -> None:
        state["log"].append(rec)
        state["evaluated"].add(rec["sequence"])

    def maybe_promote(rec: Dict[str, Any], rnd: int) -> bool:
        """Noise-aware champion promotion (Alg. 3). Returns True if the champion was updated."""
        champ = state["champion"]
        if champ is None:
            state["champion"] = rec
            state["forum"].append(f"Round {rnd}: champion initialized — {rec['sequence']} "
                                  f"(fitness {rec['fitness']:.2f}).")
            return True
        delta = rec["fitness"] - champ["fitness"]
        sigma = max(rec.get("sem", 0.0), champ.get("sem", 0.0), 0.5)  # 0.5 kcal/mol noise floor
        if delta > sigma:
            state["champion"] = rec
            state["forum"].append(f"Round {rnd}: new champion {rec['sequence']} "
                                  f"(fitness {rec['fitness']:.2f}, Δ {delta:+.2f} > noise {sigma:.2f}).")
            return True
        if delta > 0:
            # Borderline gain (within the noise band): confirm before promoting. Docking is
            # cached/deterministic per pose, so a second independent seed is only meaningful when
            # the candidate was MD-refined — re-run one MD replica and require it to still beat p*.
            confirm = None
            if rec["refined"]:
                try:
                    agg = md_eval.evaluate_replicas(
                        rec["sequence"], dock_cache[rec["sequence"]].score, n_replicas=1,
                        engine=md_engine, workdir=workdir / "md" / (rec["sequence"] + "_confirm"),
                        peptide_pdb=dock_cache[rec["sequence"]].peptide_pdb,
                        pose_pdbqt=dock_cache[rec["sequence"]].pose_pdbqt, gpu_id=gpu_id,
                        md_length_ns=md_length_ns,
                        settings={**settings, "compound_file": config["compound_file"]},
                        log=lambda m: None)
                    confirm = -agg["dg"]
                except Exception:  # noqa: BLE001
                    confirm = None
            if confirm is not None and confirm > champ["fitness"]:
                state["champion"] = rec
                state["forum"].append(f"Round {rnd}: champion {rec['sequence']} promoted after "
                                      f"second-seed confirmation (fitness {rec['fitness']:.2f}).")
                return True
        return False

    # Post-allocation: ALWAYS release the GPU + set a terminal status.
    try:
        # ── bootstrap: evaluate the seeds, set the initial champion ──
        reporter.set_status(design_id, "running_md")
        log(f"Bootstrapping with {len(seeds)} seed sequence(s).")
        round_idx = 0
        boot = [s for s in dict.fromkeys(seeds) if _valid(s, peptide_length)]
        for rec in _eval_batch(boot, "seed", round_idx, evaluate, refine=(eval_mode != "screen_only")):
            commit(rec)
            maybe_promote(rec, round_idx)
        _persist_round(reporter, design_id, round_idx, state, records)

        # ── discussion / self-organization: propose research directions ──
        _check_cancelled(reporter, design_id)
        state["directions"] = _discuss_directions(settings, task=task, peptide_length=peptide_length,
                                                  seeds=seeds, state=state, k=k_directions)
        state["forum"].append("Discussion: directions — " +
                             "; ".join(f"{d['axis']}" for d in state["directions"]))
        log("Research directions: " + "; ".join(d["axis"] for d in state["directions"]))

        # ── execution rounds ──
        for round_idx in range(1, max_rounds + 1):
            _check_cancelled(reporter, design_id)
            reporter.set_status(design_id, "running_md", current_generation=round_idx)
            props = _propose_candidates(settings, task=task, peptide_length=peptide_length,
                                        directions=state["directions"], state=state, n_total=per_round)
            # Drop already-evaluated; LLM-empty or all-dup → guided mutation of the champion.
            fresh = [p for p in props if p["sequence"] not in state["evaluated"]]
            if not fresh and state["champion"]:
                base = state["champion"]["sequence"]
                fresh = [{"sequence": _mutate(base, round_idx * 10 + j), "axis": "fallback-mutation",
                          "rationale": "LLM proposal unavailable; guided mutation of the champion."}
                         for j in range(per_round)]
                fresh = [p for p in fresh if _valid(p["sequence"], peptide_length)
                         and p["sequence"] not in state["evaluated"]]
            log(f"Round {round_idx}: evaluating {len(fresh)} candidate(s) across "
                f"{len({p['axis'] for p in fresh})} direction(s).")
            # In hybrid mode dock everything but MD-refine only the docking-promising half; md_only MDs all.
            refine_all = eval_mode == "md_only"
            seqs = [p["sequence"] for p in fresh]
            axis_of = {p["sequence"]: p["axis"] for p in fresh}
            batch = _eval_batch(seqs, axis_of, round_idx, evaluate, refine=refine_all)
            if not refine_all and batch:
                batch.sort(key=lambda r: r["docking_score"])  # most negative first
                keep = max(1, len(batch) // 2)
                for r in batch[:keep]:
                    _refine_record(r, round_idx, workdir, md_engine, n_replicas, md_length_ns,
                                   dock_cache, settings, config, log)
            improved = False
            for rec in batch:
                commit(rec)
                if maybe_promote(rec, round_idx):
                    improved = True
            _persist_round(reporter, design_id, round_idx, state, records)
            reporter.set_progress(design_id, round(min(99.0, round_idx / (max_rounds + 1) * 100), 1),
                                  current_generation=round_idx)
            ch = state["champion"]
            log(f"Round {round_idx} done — champion {ch['sequence'] if ch else '—'} "
                f"(fitness {ch['fitness']:.2f})." if ch else f"Round {round_idx} done.")

            # ── stagnation → reorganize (self-reorganization) ──
            rounds_since_improve = 0 if improved else rounds_since_improve + 1
            if rounds_since_improve >= stagnation_window and round_idx < max_rounds:
                log(f"Stagnation ({rounds_since_improve} rounds w/o improvement) — reorganizing directions.")
                new_dirs, retired = _reorganize(settings, task=task, peptide_length=peptide_length,
                                                directions=state["directions"], state=state, k=k_directions)
                state["dead_ends"].extend(retired or [d["axis"] for d in state["directions"]
                                                       if d["axis"] not in {n["axis"] for n in new_dirs}])
                state["directions"] = new_dirs
                state["forum"].append(f"Round {round_idx}: reorganized — retired "
                                     f"{retired or '(stagnant axes)'}; new {[d['axis'] for d in new_dirs]}.")
                rounds_since_improve = 0

        # ── finalize ──
        result_dict = _finalize(state, records, peptide_length, max_rounds, k_directions, eval_mode,
                                dock_engine, md_engine)
        (workdir / "design_result.json").write_text(json.dumps(result_dict, indent=2))
        # Auto-report (reuses the design report; includes the AutoScientist artifacts block).
        if str(settings.get("REPORT_ENABLED", "true")).strip().lower() not in ("0", "false", "no", "off"):
            try:
                from mdworker.report.builder import build_design_report
                (workdir / "report.html").write_text(
                    build_design_report(workdir, config, settings, result_dict), encoding="utf-8")
                log("Wrote AutoScientist report.html.")
            except Exception as exc:  # noqa: BLE001
                log(f"Report generation skipped: {exc}", level="warning")

        champ = state["champion"] or {}
        reporter.set_result(design_id, best_sequence=champ.get("sequence"),
                            best_fitness=champ.get("fitness") or 0.0,
                            best_docking_score=champ.get("docking_score"),
                            best_md_dg=champ.get("md_dg"), result_path=str(workdir))
        reporter.set_status(design_id, "completed")
        log(f"AutoScientist complete. Champion {champ.get('sequence')} "
            f"(fitness {champ.get('fitness')}, ΔG {champ.get('md_dg')}, dock {champ.get('docking_score')}); "
            f"{len(state['log'])} candidates evaluated, {len(state['dead_ends'])} dead-end directions.")
        return result_dict
    except AutoScientistCancelled:
        reporter.set_status(design_id, "cancelled")
        log("AutoScientist run cancelled.", level="warning")
        raise
    except Exception as exc:  # noqa: BLE001
        reporter.set_status(design_id, "failed", error_message=str(exc))
        log(f"AutoScientist run failed: {exc}", level="error")
        raise
    finally:
        if gpu_id is not None:
            reporter.release_gpu(design_id)


def _eval_batch(seqs, axis, rnd, evaluate, *, refine: bool) -> List[Dict[str, Any]]:
    """Evaluate candidates concurrently (docking is CPU-bound). ``axis`` is a str or a
    {sequence: axis} map. Failed candidates are dropped."""
    out: List[Dict[str, Any]] = []
    seqs = [s for s in dict.fromkeys(seqs)]
    if not seqs:
        return out
    with ThreadPoolExecutor(max_workers=min(8, len(seqs))) as ex:
        futs = {ex.submit(evaluate, s, (axis if isinstance(axis, str) else axis.get(s, "")), rnd,
                          refine=refine): s for s in seqs}
        for fut in as_completed(futs):
            rec = fut.result()
            if rec is not None:
                out.append(rec)
    return out


def _refine_record(rec, rnd, workdir, md_engine, n_replicas, md_length_ns, dock_cache, settings, config, log):
    """Add an MD ΔG to an already-docked record (hybrid mode top-half refinement)."""
    seq = rec["sequence"]
    res = dock_cache.get(seq)
    if res is None:
        return
    try:
        agg = md_eval.evaluate_replicas(
            seq, res.score, n_replicas=n_replicas, engine=md_engine,
            workdir=workdir / "md" / seq, peptide_pdb=res.peptide_pdb, pose_pdbqt=res.pose_pdbqt,
            gpu_id=None, md_length_ns=md_length_ns,
            settings={**settings, "compound_file": config["compound_file"]}, log=lambda m: log(m))
        rec["md_dg"] = agg["dg"]
        rec["sem"] = agg.get("sem", 0.0)
        rec["refined"] = True
        rec["fitness"] = round(-agg["dg"], 3)
    except Exception as exc:  # noqa: BLE001
        log(f"MD refinement failed for {seq}: {exc}", level="warning")


def _persist_round(reporter, design_id: str, rnd: int, state: Dict[str, Any],
                   records: List[Dict[str, Any]]) -> None:
    """Persist this round's candidates + a best-so-far record (GA-compatible)."""
    rows = [{"generation": r["generation"], "sequence": r["sequence"],
             "docking_score": r.get("docking_score"), "md_dg": r.get("md_dg"),
             "fitness": r.get("fitness", 0.0), "refined": r.get("refined", False)}
            for r in state["log"] if r["generation"] == rnd]
    if rows:
        reporter.record_candidates(design_id, rows)
    champ = state["champion"]
    if champ:
        records.append({"generation": rnd, "best_sequence": champ["sequence"],
                        "best_fitness": round(champ["fitness"], 3),
                        "best_md_dg": champ.get("md_dg")})


def _finalize(state, records, peptide_length, max_rounds, k_directions, eval_mode,
              dock_engine, md_engine) -> Dict[str, Any]:
    champ = state["champion"] or {}
    # best-so-far monotone convergence curve
    best = -1e18
    gens = []
    for r in records:
        best = max(best, r["best_fitness"])
        gens.append({"generation": r["generation"], "best_fitness": best,
                     "best_md_dg": r.get("best_md_dg")})
    return {
        "best_sequence": champ.get("sequence"),
        "best_fitness": champ.get("fitness"),
        "best_docking_score": champ.get("docking_score"),
        "best_md_dg": champ.get("md_dg"),
        "generations": gens,
        "candidates": [{"generation": r["generation"], "sequence": r["sequence"],
                        "docking_score": r.get("docking_score"), "md_dg": r.get("md_dg"),
                        "fitness": r.get("fitness"), "refined": r.get("refined", False),
                        "direction": r.get("direction")} for r in state["log"]],
        "autoscientist": {
            "strategy": "AutoScientists (self-organizing agent team, arXiv:2605.28655)",
            "n_directions": k_directions,
            "directions": state["directions"],
            "dead_end_directions": state["dead_ends"],
            "forum": state["forum"][-40:],
            "n_candidates": len(state["log"]),
            "champion_recipe": ({"sequence": champ.get("sequence"),
                                 "direction": champ.get("direction"),
                                 "fitness": champ.get("fitness")} if champ else None),
        },
    }


def _check_cancelled(reporter, design_id: str) -> None:
    if reporter.is_cancelled(design_id):
        raise AutoScientistCancelled(f"AutoScientist {design_id} cancelled.")
