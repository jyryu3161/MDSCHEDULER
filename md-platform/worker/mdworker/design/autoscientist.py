"""AutoScientist peptide-design orchestrator — FULL multi-agent team (Gao, Fang & Zitnik 2026,
arXiv:2605.28655).

Faithful adaptation of AUTOSCIENTISTS to peptide–compound binder design. Unlike the genetic
algorithm (random mutation/crossover) this is a *decentralized, self-organizing team* of LLM
agents (Gemini, the same key the reports use) coordinated through a file-backed SHARED STATE
rather than a central planner. The control loop is a thin heartbeat dispatcher — it rotates
agents and checks the budget; every scientific decision (directions, proposals, promotions,
reorganization) is made by the agents.

Implements the paper's algorithms:
  • Shared state S — champion p*, experiment log L, research forum F (structured posts), and the
    roster R of teams with per-team queues Q_k + dead-end registries D_k. Persisted as files under
    <workdir>/autoscientist/ (list–decide–read; sequential rotation needs no locking).
  • Heartbeat dispatch (Alg 1) — each agent invocation reads S and takes ONE action: discussion if
    a DISCUSSION-TRIGGER is open or the roster is empty; else its role cycle.
  • Self-organized team formation (Alg 2) — multiple analyst agents post candidate directions,
    ranked hypotheses, and critiques over discussion rounds, then vote DISCUSS-MORE/DONE; on a DONE
    majority the alphabetically-last analyst consolidates the roster R = {(team, axis, members)}.
  • Analyst cycle (Alg 4) — audit untested directions, rank by observed effect, propose candidate
    sequences to the team queue Q_k, and post a DISCUSSION-TRIGGER when the team stagnates.
  • Experiment cycle (Alg 3) — claim a queued candidate, evaluate it with the EXISTING docking
    (+ MD/MM-GBSA) machinery, and promote the champion only past a noise-aware gate (re-confirm a
    borderline gain on a second seed); record to L and post a RESULT to F.

Parallelism: experiment agents in a heartbeat pass evaluate their claimed candidates concurrently
(the paper's parallel experiments). The run emits the SAME DesignResult shape as the GA (so the
existing persistence, leaderboard, convergence figure, and report all work), plus a rich
``autoscientist`` block (roster/teams/agents/forum/dead-ends). Every LLM call degrades gracefully
(Gemini down → default directions + guided mutation); no single candidate aborts the run.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import docking, md_eval

_AA = "ACDEFGHIKLMNPQRSTVWY"
_AA_SET = set(_AA)
_NOISE_FLOOR = 0.5  # kcal/mol: minimum σ for the promotion gate when replicas don't estimate it


class AutoScientistCancelled(Exception):
    """Raised to abort an AutoScientist run when the backend has cancelled it."""


# ───────────────────────────── sequence helpers ─────────────────────────────

def _valid(seq: str, length: int) -> bool:
    seq = (seq or "").strip().upper()
    return len(seq) == length and all(c in _AA_SET for c in seq)


def _mutate(seq: str, salt: int, n_mut: int = 1) -> str:
    """Deterministic guided mutation (fallback when an agent's LLM proposal is unavailable)."""
    chars = list(seq)
    L = len(chars)
    for m in range(max(1, n_mut)):
        pos = (salt * 7 + m * 13) % L
        cur = chars[pos]
        chars[pos] = _AA[(_AA.index(cur) + 1 + (salt + m)) % len(_AA)] if cur in _AA_SET else "A"
    return "".join(chars)


# ───────────────────────────── shared state ─────────────────────────────

class SharedState:
    """File-backed shared state S accessible to every agent (champion, log L, forum F, roster R,
    dead-ends), persisted under <workdir>/autoscientist/ so a run is fully auditable."""

    def __init__(self, root: Path):
        self.dir = Path(root) / "autoscientist"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.champion: Optional[Dict[str, Any]] = None
        self.log: List[Dict[str, Any]] = []          # experiment log L
        self.evaluated: set = set()                   # sequences already evaluated (dedup)
        self.forum: List[Dict[str, Any]] = []         # structured posts F
        self.roster: List[Dict[str, Any]] = []        # teams R (each: id, axis, hypothesis, queue, dead)
        self.dead_ends: List[str] = []                # retired direction axes

    def post(self, agent: str, ptype: str, content: str, *, rnd: int = 0) -> None:
        self.forum.append({"agent": agent, "type": ptype, "content": content[:300], "round": rnd})

    def persist(self) -> None:
        (self.dir / "champion.json").write_text(json.dumps(self.champion, indent=2))
        (self.dir / "forum.jsonl").write_text("\n".join(json.dumps(p) for p in self.forum))
        (self.dir / "roster.json").write_text(json.dumps(self.roster, indent=2))
        (self.dir / "dead_ends.json").write_text(json.dumps(self.dead_ends))
        (self.dir / "log.jsonl").write_text("\n".join(json.dumps(r) for r in self.log))


# ───────────────────────────── Gemini agents ─────────────────────────────

def _safe_generate_json(prompt: str, settings, max_output_tokens: int) -> Optional[Dict[str, Any]]:
    """Call Gemini, return the parsed dict or None on ANY failure (so agents fall back)."""
    try:
        from mdworker.report import gemini
        return gemini.generate_json(prompt, settings=settings, max_output_tokens=max_output_tokens)
    except Exception:  # noqa: BLE001
        return None


def _log_summary(state: SharedState, limit: int = 20) -> Dict[str, Any]:
    ranked = sorted([r for r in state.log if r.get("fitness") is not None],
                    key=lambda r: r["fitness"], reverse=True)[:limit]
    champ = state.champion
    return {
        "champion": ({"sequence": champ["sequence"], "fitness": round(champ["fitness"], 3),
                      "docking_score": champ.get("docking_score"), "md_dg": champ.get("md_dg")}
                     if champ else None),
        "best_evaluated": [{"sequence": r["sequence"], "fitness": round(r["fitness"], 3),
                            "docking_score": r.get("docking_score"), "md_dg": r.get("md_dg"),
                            "team": r.get("direction")} for r in ranked],
        "n_evaluated": len(state.log),
    }


_DEFAULT_DIRECTIONS: List[Dict[str, str]] = [
    {"axis": "hydrophobic-core", "hypothesis": "Strengthen hydrophobic packing against the ligand."},
    {"axis": "aromatic-stacking", "hypothesis": "Introduce aromatic residues for π-stacking."},
    {"axis": "charge-complementarity", "hypothesis": "Match charges to the ligand's polar groups."},
    {"axis": "hbond-network", "hypothesis": "Add H-bond donors/acceptors at the interface."},
    {"axis": "turn-motif", "hypothesis": "Promote a turn/helix that presents side chains to the ligand."},
    {"axis": "terminal-anchoring", "hypothesis": "Anchor the termini to deepen the binding pose."},
]


def _discussion_turn(agent: str, state: SharedState, *, task: str, peptide_length: int,
                     seeds: List[str], prior: List[Dict[str, Any]], settings) -> Dict[str, Any]:
    """One analyst's discussion contribution (Alg 2): propose directions + ranked hypotheses,
    critique prior posts, and cast a DISCUSS-MORE / DISCUSS-DONE vote."""
    prompt = (
        f"You are analyst agent '{agent}' in an AutoScientists peptide-design team forming research "
        "directions for a binder-optimization campaign. Read the task, the current evidence, and the "
        "other agents' posts so far, then contribute to the discussion.\n\n"
        f"TASK: {task}\nPeptide length (fixed): {peptide_length}\nSeeds: {seeds}\n"
        f"Evidence: {json.dumps(_log_summary(state), default=str)}\n"
        f"Other agents' posts this discussion: {json.dumps(prior[-12:], default=str)}\n\n"
        "Return JSON: {\"directions\": [{\"axis\": short label, \"hypothesis\": one-sentence "
        "mechanism}], \"critique\": one sentence on gaps/overlaps in the posts so far, \"vote\": "
        "\"DISCUSS-MORE\" or \"DISCUSS-DONE\"}. Propose 1–2 directions grounded in peptide–ligand "
        "binding chemistry; vote DISCUSS-DONE only if the directions already cover the space well."
    )
    out = _safe_generate_json(prompt, settings, 4096)
    if not isinstance(out, dict):
        return {"agent": agent, "directions": [], "critique": "", "vote": "DISCUSS-DONE"}
    dirs = [{"axis": str(d.get("axis", ""))[:60], "hypothesis": str(d.get("hypothesis", ""))[:240]}
            for d in (out.get("directions") or []) if d.get("axis")]
    return {"agent": agent, "directions": dirs, "critique": str(out.get("critique", ""))[:240],
            "vote": "DISCUSS-DONE" if str(out.get("vote", "")).upper().endswith("DONE") else "DISCUSS-MORE"}


def _consolidate_roster(consolidator: str, contributions: List[Dict[str, Any]], *, k: int,
                        settings, task: str) -> List[Dict[str, str]]:
    """The alphabetically-last analyst consolidates the discussion into a roster of k teams (Alg 2)."""
    all_dirs = [d for c in contributions for d in c.get("directions", [])]
    prompt = (
        f"You are analyst '{consolidator}', the consolidator for an AutoScientists discussion. "
        f"Merge the team's proposed research directions into EXACTLY {k} distinct, non-overlapping "
        "teams (one mechanistic direction each) for the next experiment phase.\n\n"
        f"TASK: {task}\nProposed directions: {json.dumps(all_dirs, default=str)}\n\n"
        f"Return JSON: {{\"teams\": [{{\"axis\": short label, \"hypothesis\": one sentence}}]}} with "
        f"EXACTLY {k} teams."
    )
    out = _safe_generate_json(prompt, settings, 4096)
    teams = (out or {}).get("teams") if isinstance(out, dict) else None
    if not teams:
        # Fallback: dedup proposed directions, top up from defaults.
        seen, merged = set(), []
        for d in all_dirs + _DEFAULT_DIRECTIONS:
            ax = d.get("axis", "").strip()
            if ax and ax.lower() not in seen:
                seen.add(ax.lower()); merged.append({"axis": ax[:60], "hypothesis": d.get("hypothesis", "")[:240]})
            if len(merged) >= k:
                break
        return merged[:k] or _DEFAULT_DIRECTIONS[:k]
    return [{"axis": str(t.get("axis", f"team-{i+1}"))[:60], "hypothesis": str(t.get("hypothesis", ""))[:240]}
            for i, t in enumerate(teams[:k])]


def _analyst_propose(agent: str, team: Dict[str, Any], state: SharedState, *, task: str,
                     peptide_length: int, n: int, settings) -> List[Dict[str, str]]:
    """Analyst cycle (Alg 4): propose n candidate sequences for the team's direction, critiquing
    weak ideas before they cost compute and avoiding evaluated / dead-end sequences."""
    prompt = (
        f"You are analyst agent '{agent}' on AutoScientists team '{team['axis']}' "
        f"({team.get('hypothesis', '')}). Propose the most PROMISING next candidate peptides to "
        "evaluate ALONG THIS DIRECTION — critique and drop weak ideas yourself before compute. "
        "Build on the best evidence; avoid sequences already evaluated or in dead-ends.\n\n"
        f"TASK: {task}\nLength MUST be exactly {peptide_length} (20 standard amino acids only).\n"
        f"Evidence: {json.dumps(_log_summary(state), default=str)}\n"
        f"Already evaluated (avoid): {sorted(list(state.evaluated))[-30:]}\n"
        f"Dead-end directions: {state.dead_ends[-10:]}\n\n"
        f"Return JSON: {{\"proposals\": [{{\"sequence\": L={peptide_length} string, \"rationale\": "
        f"short why}}]}} with up to {n} concrete, plausible improvements (not random)."
    )
    out = _safe_generate_json(prompt, settings, 4096)
    props = (out or {}).get("proposals") if isinstance(out, dict) else None
    result: List[Dict[str, str]] = []
    if props:
        for p in props:
            seq = str(p.get("sequence", "")).strip().upper()
            if _valid(seq, peptide_length) and seq not in state.evaluated:
                result.append({"sequence": seq, "axis": team["axis"],
                               "rationale": str(p.get("rationale", ""))[:200], "by": agent})
    return result


def _analyst_stagnation(state: SharedState, team: Dict[str, Any], window: int) -> bool:
    """Alg 4 trigger: True if the team's recent experiments produced no champion-level improvement."""
    team_recs = [r for r in state.log if r.get("direction") == team["axis"]]
    if len(team_recs) < window:
        return False
    champ_fit = state.champion["fitness"] if state.champion else float("-inf")
    return all(r["fitness"] < champ_fit for r in team_recs[-window:])


def _reorganize(consolidator: str, state: SharedState, *, task: str, peptide_length: int, k: int,
                settings) -> Tuple[List[Dict[str, str]], List[str]]:
    """Mid-run reformation (Alg 2 via a fresh discussion): retire exhausted teams, form new ones."""
    prompt = (
        "An AutoScientists peptide-design run STAGNATED. As the consolidating analyst, reorganize the "
        "teams: retire exhausted directions and add new mechanistic ones to escape the local optimum.\n\n"
        f"TASK: {task}\nLength: {peptide_length}\nCurrent teams: "
        f"{json.dumps([{'axis': t['axis'], 'hypothesis': t.get('hypothesis')} for t in state.roster])}\n"
        f"Evidence: {json.dumps(_log_summary(state), default=str)}\n\n"
        f"Return JSON: {{\"retire\": [axis labels], \"teams\": [{{\"axis\", \"hypothesis\"}}]}} with "
        f"EXACTLY {k} teams (keep productive ones, add ≥1 new mechanism)."
    )
    out = _safe_generate_json(prompt, settings, 4096)
    if not isinstance(out, dict) or not out.get("teams"):
        cur = [{"axis": t["axis"], "hypothesis": t.get("hypothesis", "")} for t in state.roster]
        fresh = [d for d in _DEFAULT_DIRECTIONS if d["axis"] not in {t["axis"] for t in cur}]
        return ((cur[: max(1, k // 2)] + fresh)[:k], [])
    retire = [str(x)[:60] for x in (out.get("retire") or [])]
    teams = [{"axis": str(t.get("axis", f"team-{i+1}"))[:60], "hypothesis": str(t.get("hypothesis", ""))[:240]}
             for i, t in enumerate(out["teams"][:k])]
    return teams, retire


# ───────────────────────────── orchestrator ─────────────────────────────

def run_autoscientist(design_id: str, config: Dict[str, Any], reporter, settings: Dict[str, Any]) -> Dict[str, Any]:
    """Run one AutoScientist (multi-agent team) peptide-design campaign. Returns the serialized
    DesignResult dict (GA-compatible) with a rich ``autoscientist`` block for the research report."""
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
    task = (f"Design a {peptide_length}-residue peptide binding the target compound "
            f"'{config.get('compound_name', 'compound')}' with the most favorable binding energy "
            f"(lower docking score / MM-GBSA ΔG is better).")

    # Budget knobs (reuse DesignJob columns with AutoScientist semantics).
    max_passes = max(1, int(config.get("num_generations", 5)))            # heartbeat passes
    per_pass = max(1, int(config.get("population_size", 8)))              # experiments per pass (total)
    k_teams = max(1, min(6, int(config.get("dock_oversample", 3))))       # teams = research directions
    discuss_rounds = 2                                                    # max discussion rounds per phase
    stagnation_window = 2
    md_length_ns = float(config.get("md_length_ns", 10.0))
    n_replicas = max(1, min(5, int(config.get("n_replicas", 1) or 1)))
    exhaustiveness = int(config.get("exhaustiveness", 16))
    eval_mode = str(config.get("eval_mode", "hybrid"))
    md_engine = str(settings.get("MD_ENGINE", "mock")).lower()
    if md_engine == "auto":
        md_engine = "gromacs" if md_eval.gromacs_available() else "mock"
    dock_engine = docking.resolve_engine(str(settings.get("DOCK_ENGINE", "vina")))

    # Agent population: one analyst per team + an experiment-agent pool (paper: analysts + experiment
    # agents). Names are alphabetical so the discussion has a deterministic consolidator (last name).
    analyst_names = [f"analyst-{chr(ord('a') + i)}" for i in range(k_teams)]
    n_experiment_agents = max(k_teams, min(per_pass, 6))
    experiment_names = [f"exp-{i+1}" for i in range(n_experiment_agents)]
    log(f"Strategy: AutoScientists multi-agent team · {len(analyst_names)} analysts + "
        f"{len(experiment_names)} experiment agents · {k_teams} teams · {max_passes} heartbeat passes · "
        f"≤{per_pass} experiments/pass · eval {eval_mode} · dock {dock_engine} · MD {md_engine}.")

    try:
        log(f"Preparing target compound ligand from {config['compound_file']}.")
        ligand_pdbqt = docking.prepare_ligand(config["compound_file"], workdir / "ligand.pdbqt")
    except Exception as exc:  # noqa: BLE001
        reporter.set_status(design_id, "failed", error_message=f"Ligand prep failed: {exc}")
        if gpu_id is not None:
            reporter.release_gpu(design_id)
        raise

    state = SharedState(workdir)
    dock_cache: Dict[str, docking.DockResult] = {}
    records: List[Dict[str, Any]] = []

    # ── evaluation ("apply diff + train") — the experiment agents' compute ──
    def evaluate(seq: str, axis: str, rnd: int, *, refine: bool) -> Optional[Dict[str, Any]]:
        gen_dir = workdir / f"pass_{rnd:02d}"
        try:
            res = dock_cache.get(seq) or docking.dock_peptide_compound(
                seq, ligand_pdbqt, gen_dir, engine=dock_engine, exhaustiveness=exhaustiveness, n_poses=5)
        except Exception as exc:  # noqa: BLE001
            log(f"Docking failed for {seq}: {exc}", level="warning")
            return None
        if res is None or res.score is None:
            return None
        dock_cache[seq] = res
        rec: Dict[str, Any] = {"sequence": seq, "generation": rnd, "direction": axis,
                               "docking_score": round(float(res.score), 3), "md_dg": None,
                               "refined": False, "sem": 0.0}
        if refine:
            try:
                agg = md_eval.evaluate_replicas(
                    seq, res.score, n_replicas=n_replicas, engine=md_engine,
                    workdir=workdir / "md" / seq, peptide_pdb=res.peptide_pdb,
                    pose_pdbqt=res.pose_pdbqt, gpu_id=gpu_id, md_length_ns=md_length_ns,
                    settings={**settings, "compound_file": config["compound_file"]}, log=lambda m: log(m))
                rec["md_dg"] = agg["dg"]; rec["sem"] = agg.get("sem", 0.0); rec["refined"] = True
            except Exception as exc:  # noqa: BLE001
                log(f"MD evaluation failed for {seq}: {exc}", level="warning")
        rec["fitness"] = round(-(rec["md_dg"] if rec["md_dg"] is not None else rec["docking_score"]), 3)
        return rec

    def promote_gate(rec: Dict[str, Any], agent: str, rnd: int) -> bool:
        """Experiment agent's noise-aware promotion (Alg 3)."""
        champ = state.champion
        if champ is None:
            state.champion = rec
            state.post(agent, "PROMOTION", f"champion initialized {rec['sequence']} "
                       f"(fitness {rec['fitness']:.2f})", rnd=rnd)
            return True
        delta = rec["fitness"] - champ["fitness"]
        sigma = max(rec.get("sem", 0.0), champ.get("sem", 0.0), _NOISE_FLOOR)
        if delta > sigma:
            state.champion = rec
            state.post(agent, "PROMOTION", f"new champion {rec['sequence']} "
                       f"(fitness {rec['fitness']:.2f}, Δ{delta:+.2f}>σ{sigma:.2f})", rnd=rnd)
            return True
        if delta > 0 and rec["refined"]:
            try:  # borderline: confirm on a second independent MD seed before promotion
                agg = md_eval.evaluate_replicas(
                    rec["sequence"], dock_cache[rec["sequence"]].score, n_replicas=1, engine=md_engine,
                    workdir=workdir / "md" / (rec["sequence"] + "_confirm"),
                    peptide_pdb=dock_cache[rec["sequence"]].peptide_pdb,
                    pose_pdbqt=dock_cache[rec["sequence"]].pose_pdbqt, gpu_id=gpu_id,
                    md_length_ns=md_length_ns,
                    settings={**settings, "compound_file": config["compound_file"]}, log=lambda m: None)
                if -agg["dg"] > champ["fitness"]:
                    state.champion = rec
                    state.post(agent, "PROMOTION", f"champion {rec['sequence']} confirmed on 2nd seed", rnd=rnd)
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    def commit(rec: Dict[str, Any]) -> None:
        state.log.append(rec); state.evaluated.add(rec["sequence"])

    # Post-allocation: ALWAYS release the GPU + set a terminal status.
    try:
        # ── bootstrap: evaluate seeds, set initial champion ──
        reporter.set_status(design_id, "running_md")
        log(f"Bootstrapping with {len(seeds)} seed sequence(s).")
        boot = [s for s in dict.fromkeys(seeds) if _valid(s, peptide_length)]
        for rec in _eval_batch(boot, "seed", 0, evaluate, refine=(eval_mode != "screen_only")):
            commit(rec); promote_gate(rec, "exp-1", 0)
        _persist_round(reporter, design_id, 0, state, records)

        # ── discussion / self-organization (Alg 2): analysts debate + vote → roster R ──
        _check_cancelled(reporter, design_id)
        _discussion_phase(state, analyst_names, task=task, peptide_length=peptide_length,
                          seeds=seeds, k=k_teams, rounds=discuss_rounds, settings=settings, log=log)

        # ── heartbeat passes (Alg 1 dispatch over the roster) ──
        rounds_since_improve = 0
        for rnd in range(1, max_passes + 1):
            _check_cancelled(reporter, design_id)
            reporter.set_status(design_id, "running_md", current_generation=rnd)

            # Analyst heartbeats (Alg 4): each analyst proposes to its team's queue; flags stagnation.
            triggered = False
            per_team = max(1, per_pass // max(1, len(state.roster)))
            for ai, team in enumerate(state.roster):
                analyst = analyst_names[ai % len(analyst_names)]
                if _analyst_stagnation(state, team, stagnation_window):
                    state.post(analyst, "DISCUSSION-TRIGGER", f"team '{team['axis']}' stagnated", rnd=rnd)
                    triggered = True
                props = _analyst_propose(analyst, team, state, task=task, peptide_length=peptide_length,
                                         n=per_team, settings=settings)
                if not props:  # LLM empty → guided mutation of the champion (graceful degradation)
                    base = ((state.champion or {}).get("sequence")
                            or (seeds[0] if seeds else _AA[0] * peptide_length))
                    props = [{"sequence": _mutate(base, rnd * 17 + ai * 5 + j), "axis": team["axis"],
                              "rationale": "guided mutation (LLM proposal unavailable)", "by": analyst}
                             for j in range(per_team)]
                team.setdefault("queue", [])
                for p in props:
                    if p["sequence"] not in state.evaluated and _valid(p["sequence"], peptide_length):
                        team["queue"].append(p)

            # Experiment heartbeats (Alg 3) IN PARALLEL: each experiment agent claims one queued
            # candidate (round-robin across teams), evaluates it, applies the noise gate, posts RESULT.
            claims: List[Tuple[str, Dict[str, str]]] = []  # (experiment_agent, proposal)
            ei = 0
            for team in state.roster:
                q = team.get("queue", [])
                while q and len([c for c in claims]) < per_pass:
                    claims.append((experiment_names[ei % len(experiment_names)], q.pop(0))); ei += 1
                    if len(claims) >= per_pass:
                        break
                if len(claims) >= per_pass:
                    break
            refine_all = eval_mode == "md_only"
            batch = _eval_batch([c[1]["sequence"] for c in claims],
                                {c[1]["sequence"]: c[1]["axis"] for c in claims}, rnd, evaluate,
                                refine=refine_all)
            if not refine_all and batch:  # hybrid: MD-refine the docking-promising half
                batch.sort(key=lambda r: r["docking_score"])
                for r in batch[: max(1, len(batch) // 2)]:
                    _refine_record(r, workdir, md_engine, n_replicas, md_length_ns, dock_cache,
                                   settings, config, gpu_id, log)
            agent_of = {c[1]["sequence"]: c[0] for c in claims}
            improved = False
            for rec in batch:
                commit(rec)
                ag = agent_of.get(rec["sequence"], "exp-1")
                state.post(ag, "RESULT", f"{rec['sequence']} fitness {rec['fitness']:.2f} "
                           f"(dock {rec['docking_score']}, team {rec['direction']})", rnd=rnd)
                if promote_gate(rec, ag, rnd):
                    improved = True
            _persist_round(reporter, design_id, rnd, state, records)
            reporter.set_progress(design_id, round(min(99.0, rnd / (max_passes + 1) * 100), 1),
                                  current_generation=rnd)
            state.persist()
            ch = state.champion
            log(f"Pass {rnd}: champion {ch['sequence'] if ch else '—'} "
                f"(fitness {ch['fitness']:.2f}); {len(batch)} experiments." if ch
                else f"Pass {rnd}: {len(batch)} experiments.")

            # ── stagnation → self-reorganization (Alg 2 mid-run reformation) ──
            rounds_since_improve = 0 if improved else rounds_since_improve + 1
            if (triggered or rounds_since_improve >= stagnation_window) and rnd < max_passes:
                consolidator = sorted(analyst_names)[-1]
                log(f"Pass {rnd}: DISCUSSION-TRIGGER active — '{consolidator}' reorganizing teams.")
                new_teams, retired = _reorganize(consolidator, state, task=task,
                                                 peptide_length=peptide_length, k=k_teams, settings=settings)
                retired = retired or [t["axis"] for t in state.roster
                                      if t["axis"] not in {n["axis"] for n in new_teams}]
                state.dead_ends.extend(retired)
                state.roster = [{"axis": t["axis"], "hypothesis": t["hypothesis"], "queue": []}
                                for t in new_teams]
                state.post(consolidator, "ROSTER", f"reorganized: retired {retired}; "
                           f"new {[t['axis'] for t in new_teams]}", rnd=rnd)
                rounds_since_improve = 0

        # ── finalize ──
        state.persist()
        result_dict = _finalize(state, records, analyst_names, experiment_names, k_teams,
                                eval_mode, dock_engine, md_engine)
        (workdir / "design_result.json").write_text(json.dumps(result_dict, indent=2))
        if str(settings.get("REPORT_ENABLED", "true")).strip().lower() not in ("0", "false", "no", "off"):
            try:
                from mdworker.report.builder import build_design_report
                (workdir / "report.html").write_text(
                    build_design_report(workdir, config, settings, result_dict), encoding="utf-8")
                log("Wrote AutoScientist report.html.")
            except Exception as exc:  # noqa: BLE001
                log(f"Report generation skipped: {exc}", level="warning")

        champ = state.champion or {}
        reporter.set_result(design_id, best_sequence=champ.get("sequence"),
                            best_fitness=champ.get("fitness") or 0.0,
                            best_docking_score=champ.get("docking_score"),
                            best_md_dg=champ.get("md_dg"), result_path=str(workdir))
        reporter.set_status(design_id, "completed")
        log(f"AutoScientist complete. Champion {champ.get('sequence')} (fitness {champ.get('fitness')}, "
            f"ΔG {champ.get('md_dg')}, dock {champ.get('docking_score')}); {len(state.log)} candidates by "
            f"{len(analyst_names)} analysts + {len(experiment_names)} experiment agents across "
            f"{k_teams} teams; {len(state.dead_ends)} dead-end directions.")
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


def _discussion_phase(state: SharedState, analysts: List[str], *, task: str, peptide_length: int,
                      seeds: List[str], k: int, rounds: int, settings, log) -> None:
    """Self-organized team formation (Alg 2): analysts post directions/critiques + vote over rounds;
    on a DISCUSS-DONE majority the alphabetically-last analyst consolidates the roster R."""
    contributions: List[Dict[str, Any]] = []
    for dr in range(1, rounds + 1):
        votes = []
        for agent in analysts:
            c = _discussion_turn(agent, state, task=task, peptide_length=peptide_length, seeds=seeds,
                                 prior=contributions, settings=settings)
            contributions.append(c)
            for d in c["directions"]:
                state.post(agent, "DIRECTION", f"{d['axis']}: {d['hypothesis']}", rnd=0)
            if c["critique"]:
                state.post(agent, "CRITIQUE", c["critique"], rnd=0)
            state.post(agent, "VOTE", c["vote"], rnd=0)
            votes.append(c["vote"])
        if votes and sum(v == "DISCUSS-DONE" for v in votes) > len(votes) / 2:
            break
    consolidator = sorted(analysts)[-1]  # alphabetically-last analyst consolidates (paper's tie-break)
    teams = _consolidate_roster(consolidator, contributions, k=k, settings=settings, task=task)
    state.roster = [{"axis": t["axis"], "hypothesis": t["hypothesis"], "queue": []} for t in teams]
    state.post(consolidator, "ROSTER", f"teams: {[t['axis'] for t in teams]}", rnd=0)
    log(f"Discussion: {len(analysts)} analysts → {len(teams)} teams: " +
        "; ".join(t["axis"] for t in teams) + f" (consolidated by {consolidator}).")


def _eval_batch(seqs, axis, rnd, evaluate, *, refine: bool) -> List[Dict[str, Any]]:
    """Evaluate candidates concurrently (parallel experiments). ``axis`` is a str or {seq: axis}.

    Sequences are de-duplicated first, so concurrent threads never share a dock_cache key (the
    get-or-compute is therefore race-free) and docking — which tokenizes its output filenames
    per-sequence (the proven GA concurrent-docking pattern) — never collides on shared paths."""
    out: List[Dict[str, Any]] = []
    seqs = list(dict.fromkeys(seqs))
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


def _refine_record(rec, workdir, md_engine, n_replicas, md_length_ns, dock_cache, settings, config,
                   gpu_id, log):
    seq = rec["sequence"]; res = dock_cache.get(seq)
    if res is None:
        return
    try:
        agg = md_eval.evaluate_replicas(
            seq, res.score, n_replicas=n_replicas, engine=md_engine, workdir=workdir / "md" / seq,
            peptide_pdb=res.peptide_pdb, pose_pdbqt=res.pose_pdbqt, gpu_id=gpu_id,
            md_length_ns=md_length_ns, settings={**settings, "compound_file": config["compound_file"]},
            log=lambda m: log(m))
        rec["md_dg"] = agg["dg"]; rec["sem"] = agg.get("sem", 0.0); rec["refined"] = True
        rec["fitness"] = round(-agg["dg"], 3)
    except Exception as exc:  # noqa: BLE001
        log(f"MD refinement failed for {seq}: {exc}", level="warning")


def _persist_round(reporter, design_id: str, rnd: int, state: SharedState,
                   records: List[Dict[str, Any]]) -> None:
    rows = [{"generation": r["generation"], "sequence": r["sequence"],
             "docking_score": r.get("docking_score"), "md_dg": r.get("md_dg"),
             "fitness": r.get("fitness", 0.0), "refined": r.get("refined", False)}
            for r in state.log if r["generation"] == rnd]
    if rows:
        reporter.record_candidates(design_id, rows)
    if state.champion:
        records.append({"generation": rnd, "best_sequence": state.champion["sequence"],
                        "best_fitness": round(state.champion["fitness"], 3),
                        "best_md_dg": state.champion.get("md_dg")})


def _finalize(state: SharedState, records, analysts, experiments, k_teams, eval_mode,
              dock_engine, md_engine) -> Dict[str, Any]:
    champ = state.champion or {}
    best = -1e18
    gens = []
    for r in records:
        best = max(best, r["best_fitness"])
        gens.append({"generation": r["generation"], "best_fitness": best, "best_md_dg": r.get("best_md_dg")})
    return {
        "best_sequence": champ.get("sequence"),
        "best_fitness": champ.get("fitness"),
        "best_docking_score": champ.get("docking_score"),
        "best_md_dg": champ.get("md_dg"),
        "generations": gens,
        "candidates": [{"generation": r["generation"], "sequence": r["sequence"],
                        "docking_score": r.get("docking_score"), "md_dg": r.get("md_dg"),
                        "fitness": r.get("fitness"), "refined": r.get("refined", False),
                        "direction": r.get("direction")} for r in state.log],
        "autoscientist": {
            "strategy": "AutoScientists multi-agent team (self-organizing, arXiv:2605.28655)",
            "architecture": "decentralized agents + shared state; no central planner",
            "n_directions": k_teams,
            "directions": [{"axis": t["axis"], "hypothesis": t.get("hypothesis", "")} for t in state.roster],
            "dead_end_directions": state.dead_ends,
            "agents": {"analysts": analysts, "experiment_agents": experiments},
            "forum": [f"[{p['type']}] {p['agent']}: {p['content']}" for p in state.forum[-50:]],
            "n_candidates": len(state.log),
            "champion_recipe": ({"sequence": champ.get("sequence"), "direction": champ.get("direction"),
                                 "fitness": champ.get("fitness")} if champ else None),
        },
    }


def _check_cancelled(reporter, design_id: str) -> None:
    if reporter.is_cancelled(design_id):
        raise AutoScientistCancelled(f"AutoScientist {design_id} cancelled.")
