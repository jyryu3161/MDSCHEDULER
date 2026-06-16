"""Design-job orchestrator: run the PyGAD peptide-design GA with real docking + MD refinement.

Entry point ``run_design(design_id, config, reporter, settings)`` mirrors the MD pipeline's
worker↔reporter seam. The backend supplies a reporter that persists progress/candidates to the
DB and brokers the design-pool GPU; this module owns the compute:

  • prepare the target compound ligand once (RDKit + Meeko)
  • dock_batch  — Vina-dock the population in parallel across a thread pool (CPU-bound)
  • md_batch    — MD-refine the per-generation docking elites on the design GPU (md_eval),
                 mock-anchored or real GROMACS+MM/GBSA per ``md_engine``
  • drive ga.run_ga, persisting each generation's candidates and the running best

Cancellation is cooperative: the reporter's ``is_cancelled`` is polled between generations.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import docking, ga, md_eval


class DesignCancelled(Exception):
    """Raised to abort a design run when the backend has cancelled it."""


def _dock_workers(settings: dict) -> int:
    try:
        import os
        return max(1, min(8, (os.cpu_count() or 4) - 1))
    except Exception:  # noqa: BLE001
        return 4


def run_design(design_id: str, config: Dict[str, Any], reporter, settings: Dict[str, Any]) -> Dict[str, Any]:
    """Run one peptide-design GA job. Returns the serialized DesignResult dict."""
    def log(msg: str, level: str = "info") -> None:
        reporter.log(design_id, level, "design", msg)

    reporter.set_status(design_id, "preparing")
    storage_root = Path(settings.get("STORAGE_ROOT", "."))
    workdir = storage_root / "design" / design_id
    workdir.mkdir(parents=True, exist_ok=True)

    gpu_id = reporter.request_gpu(design_id)  # design pool; None -> CPU-only / mock MD
    log(f"Design GPU assignment: {gpu_id if gpu_id is not None else 'none (mock MD / CPU docking)'}.")

    initial = list(config["initial_sequences"])
    population_size = int(config.get("population_size", 10))
    num_generations = int(config.get("num_generations", 5))
    top_k_md = int(config.get("top_k_md", 2))
    md_length_ns = float(config.get("md_length_ns", 10.0))
    exhaustiveness = int(config.get("exhaustiveness", 16))
    eval_mode = str(config.get("eval_mode", "hybrid"))  # "hybrid" (dock->top-k MD) | "md_only"
    md_engine = str(settings.get("MD_ENGINE", "mock")).lower()
    if md_engine == "auto":
        md_engine = "gromacs" if md_eval.gromacs_available() else "mock"
    # Docking engine: default "vina" (AutoDock Vina 1.2.7, rigid); set DOCK_ENGINE=smina for
    # flexible receptor side chains, or "auto" to use smina when installed.
    dock_engine = docking.resolve_engine(str(settings.get("DOCK_ENGINE", "vina")))
    mode_desc = "dock all -> MD top-%d" % top_k_md if eval_mode == "hybrid" else "dock all -> MD ALL candidates"
    log(f"Eval mode: {eval_mode} ({mode_desc}); docking engine: {dock_engine} "
        f"(exhaustiveness {exhaustiveness}); MD engine: {md_engine}.")

    try:
        log(f"Preparing target compound ligand from {config['compound_file']}.")
        ligand_pdbqt = docking.prepare_ligand(config["compound_file"], workdir / "ligand.pdbqt")
    except Exception as exc:  # noqa: BLE001
        reporter.set_status(design_id, "failed", error_message=f"Ligand prep failed: {exc}")
        if gpu_id is not None:
            reporter.release_gpu(design_id)
        raise

    dock_results: Dict[str, docking.DockResult] = {}

    def dock_batch(seqs: List[str], generation: int) -> Dict[str, Optional[float]]:
        _check_cancelled(reporter, design_id)
        scores: Dict[str, Optional[float]] = {}
        gen_dir = workdir / f"gen_{generation:02d}"
        with ThreadPoolExecutor(max_workers=_dock_workers(settings)) as ex:
            futs = {
                ex.submit(docking.dock_peptide_compound, s, ligand_pdbqt, gen_dir,
                          engine=dock_engine, exhaustiveness=exhaustiveness, n_poses=5): s
                for s in seqs
            }
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    res = fut.result()
                    dock_results[s] = res
                    scores[s] = res.score
                except Exception as exc:  # noqa: BLE001 — one bad candidate must not kill the run
                    log(f"Docking failed for {s}: {exc}", level="warning")
                    scores[s] = None
        return scores

    def md_batch(seqs: List[str], generation: int) -> Dict[str, Optional[float]]:
        _check_cancelled(reporter, design_id)
        out: Dict[str, Optional[float]] = {}
        for s in seqs:
            res = dock_results.get(s)
            if res is None or res.score is None:
                out[s] = None
                continue
            try:
                out[s] = md_eval.evaluate(
                    s, res.score, engine=md_engine,
                    workdir=workdir / "md" / s, peptide_pdb=res.peptide_pdb,
                    pose_pdbqt=res.pose_pdbqt, gpu_id=gpu_id, md_length_ns=md_length_ns,
                    settings={**settings, "compound_file": config["compound_file"]},
                    log=lambda m: log(m),
                )
            except Exception as exc:  # noqa: BLE001
                log(f"MD evaluation failed for {s}: {exc}", level="warning")
                out[s] = None
        return out

    def progress(ev: dict) -> None:
        stage = ev.get("stage")
        if stage == "docking":
            reporter.set_status(design_id, "preparing", current_generation=ev["generation"])
            log(f"Generation {ev['generation']}: docking {ev['n']} candidates.")
        elif stage == "md":
            reporter.set_status(design_id, "running_md", current_generation=ev["generation"])
            log(f"Generation {ev['generation']}: MD-refining {ev['n']} elites ({md_engine}).")
        elif stage == "generation_done":
            _persist_generation(reporter, design_id, ev)
            frac = (ev["generation"] + 1) / max(1, num_generations + 1)
            reporter.set_progress(design_id, round(min(99.0, frac * 100.0), 1),
                                  current_generation=ev["generation"])
            log(f"Generation {ev['generation']} done — best {ev['best_sequence']} "
                f"(fitness {ev['best_fitness']:.3f}).")
        _check_cancelled(reporter, design_id)

    # Post-allocation run: ALWAYS release the GPU and set a terminal status, even on
    # GA/MD/persistence/cancel/result-write failure.
    try:
        reporter.set_status(design_id, "running_md")
        result = ga.run_ga(
            initial, dock_batch, md_batch,
            num_generations=num_generations, population_size=population_size,
            top_k_md=top_k_md, eval_mode=eval_mode, progress=progress,
        )
        result_dict = ga.result_to_dict(result)
        (workdir / "design_result.json").write_text(json.dumps(result_dict, indent=2))
        reporter.set_result(
            design_id, best_sequence=result.best_sequence, best_fitness=result.best_fitness,
            best_docking_score=result.best_docking_score, best_md_dg=result.best_md_dg,
            result_path=str(workdir),
        )
        reporter.set_status(design_id, "completed")
        log(f"Design complete. Best peptide {result.best_sequence} "
            f"(fitness {result.best_fitness:.3f}, ΔG {result.best_md_dg}, dock {result.best_docking_score}).")
        return result_dict
    except DesignCancelled:
        reporter.set_status(design_id, "cancelled")
        log("Design run cancelled.", level="warning")
        raise
    except Exception as exc:  # noqa: BLE001
        reporter.set_status(design_id, "failed", error_message=str(exc))
        log(f"Design run failed: {exc}", level="error")
        raise
    finally:
        if gpu_id is not None:
            reporter.release_gpu(design_id)


def _persist_generation(reporter, design_id: str, ev: dict) -> None:
    """Upsert the candidates touched this generation to the DB (each carries its own gen)."""
    rows = []
    for seq, ce in ev.get("candidates", {}).items():
        rows.append({
            "generation": ce.get("generation", ev["generation"]), "sequence": seq,
            "docking_score": ce.get("docking_score"), "md_dg": ce.get("md_dg"),
            "fitness": ce.get("fitness"), "refined": ce.get("refined", False),
        })
    if rows:
        reporter.record_candidates(design_id, rows)


def _check_cancelled(reporter, design_id: str) -> None:
    if reporter.is_cancelled(design_id):
        raise DesignCancelled(f"Design {design_id} cancelled.")
