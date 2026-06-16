"""Design-job execution seam: build the run config + settings and invoke the worker runner.

Kept separate from the router so it can run inside the queue manager's executor (local mode)
or be adapted to RQ later. Import of ``mdworker.design.runner`` is deferred to run time so the
backend stays importable when the worker package or its heavy deps (vina/rdkit/pygad) are
absent — a missing worker marks the design job failed with a clear message.
"""

from __future__ import annotations

import json

from ..config import get_settings
from ..database import SessionLocal
from ..models import DesignJob, JobStatus, utcnow
from .design_reporter import DesignReporter


def _design_config(dj: DesignJob) -> dict:
    return {
        "compound_file": dj.compound_file,
        "compound_name": dj.compound_name,
        "initial_sequences": json.loads(dj.initial_sequences),
        "peptide_length": dj.peptide_length,
        "population_size": dj.population_size,
        "num_generations": dj.num_generations,
        "top_k_md": dj.top_k_md,
        "md_length_ns": dj.md_length_ns,
        "exhaustiveness": dj.exhaustiveness,
    }


def _runner_settings() -> dict:
    s = get_settings()
    return {
        "STORAGE_ROOT": s.STORAGE_ROOT,
        "MD_ENGINE": s.resolved_md_engine(),
        "MDP_TEMPLATE_DIR": s.MDP_TEMPLATE_DIR,
        "TRAJECTORY_OUTPUT_PS": s.TRAJECTORY_OUTPUT_PS,
        "PROTEIN_FORCE_FIELD": s.PROTEIN_FORCE_FIELD,
        "LIGAND_FORCE_FIELD": s.LIGAND_FORCE_FIELD,
        "LIGAND_CHARGE_METHOD": s.LIGAND_CHARGE_METHOD,
        "WATER_MODEL": s.WATER_MODEL,
    }


def run_design_job(design_id: str) -> None:
    """Run a design job to completion in-process (called from the queue executor)."""
    reporter = DesignReporter(SessionLocal)
    db = SessionLocal()
    try:
        dj = db.get(DesignJob, design_id)
        if dj is None:
            return
        config = _design_config(dj)
    finally:
        db.close()

    try:
        from mdworker.design.runner import run_design  # deferred: worker may be absent
    except Exception as exc:  # noqa: BLE001
        reporter.log(design_id, "error", "design",
                     f"Design worker unavailable: {exc.__class__.__name__}: {exc}")
        reporter.set_status(design_id, JobStatus.FAILED,
                            error_message=f"Design worker unavailable: {exc}")
        return

    try:
        run_design(design_id, config, reporter, _runner_settings())
    except Exception as exc:  # noqa: BLE001 — runner sets terminal status; this is a backstop
        reporter.set_status(design_id, JobStatus.FAILED, error_message=str(exc))
        reporter.log(design_id, "error", "design", f"Design execution error: {exc}")
