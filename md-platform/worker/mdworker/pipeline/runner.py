"""Pipeline runner (CONTRACT §9): execute the ordered MD pipeline for one subjob (one pose).

``run_subjob(subjob_id, *, reporter, settings)`` is the single entry point used by BOTH the
rq task (HttpReporter) and the backend's in-process LocalExecutor (DbReporter). It loads the
job's metadata.json, builds a JobContext, requests a GPU lock, runs the steps in order
threading each step's outputs to the next, and reports status/progress/logs throughout. On
any failure the subjob is marked ``failed`` with the error message and the GPU lock is
released; the worker process is never crashed by a single bad job.
"""

from __future__ import annotations

import json
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from mdworker.pipeline.context import JobCancelled, JobContext
from mdworker.pipeline.steps import (
    analyze_md,
    assign_bond_orders,
    mmpbsa,
    package_results,
    parameterize_ligand,
    prepare_structure,
    render_movie,
    run_md,
    split_pdbqt_poses,
    validate_input,
)

# Statuses considered terminal (CONTRACT §4).
_FAILED = "failed"
_COMPLETED = "completed"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _parse_subjob_id(subjob_id: str) -> tuple[str, int, int]:
    """Parse a subjob id into (job_id, pose_index, replica_index).

    Replica 1 keeps the original `{job_id}_pose_{NN}` form (replica_index 1); additional MD
    replicas use `{job_id}_pose_{NN}_rep_{RR}`. job_id itself may contain underscores.
    """
    marker = "_pose_"
    if marker in subjob_id:
        job_id, tail = subjob_id.rsplit(marker, 1)
        replica = 1
        if "_rep_" in tail:
            pose_s, rep_s = tail.split("_rep_", 1)
            try:
                replica = int(rep_s)
            except ValueError:
                pose_s = tail  # malformed suffix -> treat whole tail as pose below
        else:
            pose_s = tail
        try:
            return job_id, int(pose_s), replica
        except ValueError:
            pass
    raise ValueError(
        f"Cannot parse subjob id '{subjob_id}' (expected '<job_id>_pose_<NN>[_rep_<RR>]')."
    )


def _load_metadata(storage_root: str, job_id: str) -> Dict[str, Any]:
    meta_path = Path(storage_root) / "jobs" / job_id / "metadata.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"Job metadata not found: {meta_path}")
    return json.loads(meta_path.read_text())


def _coerce_settings(settings):
    """Accept either an ``mdworker.config.Settings`` object or the UPPERCASE env-style dict the
    backend's LocalExecutor passes (CONTRACT §5 enqueue), normalizing to a Settings object so the
    pipeline can use ``settings.storage_root`` / ``settings.resolved_engine`` / etc. uniformly."""
    if hasattr(settings, "resolved_engine") and hasattr(settings, "storage_root"):
        return settings
    from mdworker.config import Settings, load_settings

    if isinstance(settings, dict):
        d = settings
        base = load_settings()  # start from env defaults, then override with provided keys

        def pick(key, default):
            return d.get(key, default)

        def as_bool(value, default):
            if isinstance(value, bool):
                return value
            if value is None:
                return default
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)

        return Settings(
            md_engine=pick("MD_ENGINE", base.md_engine),
            backend_url=pick("BACKEND_URL", base.backend_url),
            internal_api_token=pick("INTERNAL_API_TOKEN", base.internal_api_token),
            protein_force_field=pick("PROTEIN_FORCE_FIELD", base.protein_force_field),
            ligand_force_field=pick("LIGAND_FORCE_FIELD", base.ligand_force_field),
            ligand_charge_method=pick("LIGAND_CHARGE_METHOD", base.ligand_charge_method),
            water_model=pick("WATER_MODEL", base.water_model),
            protein_force_field_fallback=pick("PROTEIN_FORCE_FIELD_FALLBACK", base.protein_force_field_fallback),
            water_model_fallback=pick("WATER_MODEL_FALLBACK", base.water_model_fallback),
            forcefield_autofallback=as_bool(d.get("FORCEFIELD_AUTOFALLBACK"), base.forcefield_autofallback),
            require_ligand_chemistry=as_bool(d.get("REQUIRE_LIGAND_CHEMISTRY"), base.require_ligand_chemistry),
            allow_smiles_input=as_bool(d.get("ALLOW_SMILES_INPUT"), base.allow_smiles_input),
            allow_meeko_mapping_input=as_bool(d.get("ALLOW_MEEKO_MAPPING_INPUT"), base.allow_meeko_mapping_input),
            mdp_template_dir=pick("MDP_TEMPLATE_DIR", base.mdp_template_dir),
            storage_root=pick("STORAGE_ROOT", base.storage_root),
            trajectory_output_ps=int(pick("TRAJECTORY_OUTPUT_PS", base.trajectory_output_ps)),
            md_mock_speedup=int(pick("MD_MOCK_SPEEDUP", base.md_mock_speedup)),
            box_padding_nm=float(pick("BOX_PADDING_NM", base.box_padding_nm)),
            nvt_steps=int(pick("NVT_STEPS", base.nvt_steps)),
            npt_steps=int(pick("NPT_STEPS", base.npt_steps)),
            redis_url=pick("REDIS_URL", base.redis_url),
        )
    return load_settings()


def _normalize_inputs(job_meta: Dict[str, Any], job_dir: Path) -> Dict[str, Any]:
    """Ensure ``job_meta['inputs']`` has resolvable file paths.

    The backend writes ``input_files`` as bare filenames (copied into input/original/); the
    pipeline steps read ``inputs`` with usable paths. Build ``inputs`` from ``input_files`` +
    the job's input/original dir when absent, so both metadata shapes work."""
    if job_meta.get("inputs"):
        return job_meta
    files = job_meta.get("input_files", {}) or {}
    orig = job_dir / "input" / "original"

    def resolve(name):
        # Confine input files to the job's own input/original dir (basename only), so a
        # crafted metadata path cannot widen file access beyond this job's inputs.
        if not name:
            return None
        return str(orig / Path(name).name)

    job_meta["inputs"] = {
        "pose_file": resolve(files.get("pose_file")),
        "chemistry_file": resolve(files.get("chemistry_file")),
        "receptor_file": resolve(files.get("receptor_file")),
        "smiles": job_meta.get("smiles"),
        "meeko_mapping": bool(job_meta.get("meeko_mapping", False)),
    }
    return job_meta


def _acquire_gpu(ctx: JobContext, *, use_gpu: bool, retries: int, wait_s: float) -> Optional[int]:
    if not use_gpu:
        ctx.info("gpu", "GPU disabled for this job; running on CPU.")
        return None
    for attempt in range(max(1, retries)):
        gpu = ctx.request_gpu()
        if gpu is not None:
            ctx.info("gpu", f"Acquired GPU {gpu} for subjob.")
            return gpu
        if attempt < retries - 1:
            time.sleep(wait_s)
    ctx.warning("gpu", "No GPU available after waiting; proceeding without a GPU lock "
                       "(the mock engine runs on CPU; a real GROMACS run will be slow).")
    return None


def run_subjob(subjob_id: str, *, reporter, settings) -> Dict[str, Any]:
    """Execute the full pipeline for one pose. Returns a small result dict.

    Never raises for pipeline/job errors (they are reported as a ``failed`` subjob); only
    truly unexpected programming errors propagate after being logged.
    """
    settings = _coerce_settings(settings)
    job_id, pose_index, replica_index = _parse_subjob_id(subjob_id)
    storage_root = settings.storage_root

    try:
        job_meta = _load_metadata(storage_root, job_id)
        job_meta = _normalize_inputs(job_meta, Path(storage_root) / "jobs" / job_id)
    except Exception as exc:  # noqa: BLE001
        reporter.log(job_id, subjob_id, "error", "runner", f"Failed to load job metadata: {exc}")
        reporter.set_subjob_status(subjob_id, status=_FAILED, error_message=str(exc),
                                   completed_at=_utcnow_iso())
        return {"subjob_id": subjob_id, "status": _FAILED, "error": str(exc)}

    subjob_meta = next(
        (s for s in job_meta.get("subjobs", []) if s.get("id") == subjob_id),
        {"id": subjob_id, "pose_index": pose_index, "replica_index": replica_index,
         "docking_score": None},
    )
    # Prefer the replica index recorded in metadata; fall back to the one parsed from the id.
    replica_index = int(subjob_meta.get("replica_index", replica_index) or 1)

    ctx = JobContext(
        job_id=job_id,
        subjob_id=subjob_id,
        pose_index=pose_index,
        storage_root=storage_root,
        reporter=reporter,
        job_meta=job_meta,
        subjob_meta=subjob_meta,
        replica_index=replica_index,
    )
    ctx.ensure_dirs()

    use_gpu = bool(job_meta.get("use_gpu", True))
    assigned_gpu: Optional[int] = None

    reporter.set_subjob_status(subjob_id, status="preparing", current_step="runner",
                               progress=1.0, started_at=_utcnow_iso())
    ctx.info("runner", f"Starting pipeline for {subjob_id} (pose {pose_index}, "
                       f"engine={settings.resolved_engine}, md={job_meta.get('md_length_ns')} ns).")

    try:
        assigned_gpu = _acquire_gpu(ctx, use_gpu=use_gpu, retries=15, wait_s=4.0)
        ctx.subjob_meta["assigned_gpu"] = assigned_gpu
        if assigned_gpu is not None:
            reporter.set_subjob_status(subjob_id, assigned_gpu=assigned_gpu)

        # --- ordered pipeline (CONTRACT §10.1) ---
        # check_cancelled() between steps aborts promptly on a user/admin cancel; run_md also
        # polls mid-run and kills the gmx subprocess (engine/gromacs.py) so a long MD stops.
        ctx.check_cancelled()
        validate_input.run(ctx, settings)
        split_pdbqt_poses.run(ctx, settings)
        ctx.check_cancelled()
        bonds = assign_bond_orders.run(ctx, settings)
        prep = prepare_structure.run(ctx, settings)
        params = parameterize_ligand.run(ctx, settings, bond_orders=bonds, prepared=prep)
        ctx.check_cancelled()
        md = run_md.run(ctx, settings, prepared=prep, ligand=params,
                        bond_orders=bonds, assigned_gpu=assigned_gpu)
        ctx.check_cancelled()
        analysis = analyze_md.run(ctx, settings, md=md)
        ctx.check_cancelled()
        render_movie.run(ctx, settings, md=md)
        ctx.check_cancelled()
        # Optional quantitative binding ΔG (MM/PBSA & MM/GBSA) — opt-in via job
        # compute_mmpbsa, meaningful only for a stably-bound production trajectory. Never
        # fails the job (skips gracefully if gmx_MMPBSA/MPI is unavailable).
        mmpbsa.run(ctx, settings, md=md)
        ctx.check_cancelled()
        pkg = package_results.run(ctx, settings, bond_orders=bonds, prepared=prep,
                                  params=params, md=md, analysis=analysis)

        reporter.set_subjob_status(
            subjob_id,
            status=_COMPLETED,
            current_step="done",
            progress=100.0,
            completed_ns=md.get("completed_ns"),
            ns_per_day=md.get("ns_per_day"),
            result_path=pkg.get("result_path"),
            completed_at=_utcnow_iso(),
        )
        ctx.info("runner", f"Pipeline completed for {subjob_id}.")
        return {"subjob_id": subjob_id, "status": _COMPLETED, "result_path": pkg.get("result_path")}

    except JobCancelled as exc:
        # Cancelled by the user/admin: finalize as cancelled (NOT failed). The GPU lock is
        # released in `finally`; any gmx subprocess was already killed by the engine.
        ctx.info("runner", f"Pipeline aborted by cancel: {exc}")
        reporter.set_subjob_status(
            subjob_id, status="cancelled", current_step="cancelled",
            completed_at=_utcnow_iso(),
        )
        return {"subjob_id": subjob_id, "status": "cancelled"}

    except Exception as exc:  # noqa: BLE001 - report as failed, do not crash the worker
        tb = traceback.format_exc(limit=6)
        msg = str(exc) or exc.__class__.__name__
        try:
            ctx.error("runner", f"Pipeline failed: {msg}\n{tb}")
        except Exception:  # noqa: BLE001
            reporter.log(job_id, subjob_id, "error", "runner", f"Pipeline failed: {msg}")
        reporter.set_subjob_status(subjob_id, status=_FAILED, error_message=msg,
                                   completed_at=_utcnow_iso())
        return {"subjob_id": subjob_id, "status": _FAILED, "error": msg}
    finally:
        if assigned_gpu is not None:
            try:
                ctx.release_gpu()
            except Exception:  # noqa: BLE001
                pass
