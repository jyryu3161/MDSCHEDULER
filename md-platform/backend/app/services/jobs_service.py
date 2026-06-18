"""Job orchestration helpers: id generation, job+subjob creation, metadata.

Centralizes the logic shared by the jobs router so the route handlers stay thin
and the queue/realtime side-effects are consistent.
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    ChemSource,
    InputType,
    Job,
    JobStatus,
    LigandType,
    Priority,
    SubJob,
)
from ..schemas import JobCreate, ValidationReport
from . import gpu_manager, storage

# Serializes the per-day job sequence counter to avoid duplicate ids under load.
_id_lock = threading.Lock()

# Preset -> ns (CONTRACT §6 / PDR). custom uses md_length_ns verbatim.
_PRESET_NS = {"quick": 10, "standard": 50, "extended": 100}


def resolve_md_length(create: JobCreate, default_ns: int) -> int:
    preset = (create.md_preset or "standard").lower()
    if preset in _PRESET_NS:
        return _PRESET_NS[preset]
    if preset == "custom":
        return int(create.md_length_ns)
    return int(default_ns)


def _next_job_id(db: Session) -> str:
    """Compute the next ``job_YYYYMMDD_NNN`` id by probing existing rows.

    Caller MUST hold ``_id_lock`` and commit the inserted row before releasing it,
    otherwise two concurrent callers could compute the same id.
    """
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    prefix = f"job_{today}_"
    count = db.execute(
        select(func.count()).select_from(Job).where(Job.id.like(f"{prefix}%"))
    ).scalar_one()
    seq = int(count) + 1
    while db.get(Job, f"{prefix}{seq:03d}") is not None:
        seq += 1
    return f"{prefix}{seq:03d}"


def subjob_id(job_id: str, pose_index: int, replica_index: int = 1) -> str:
    """Subjob id. Replica 1 keeps the original `{job}_pose_{NN}` form (so single-replica jobs are
    unchanged); extra MD replicas use `{job}_pose_{NN}_rep_{RR}`. MUST stay in sync with the
    worker's runner._parse_subjob_id()."""
    if replica_index and int(replica_index) > 1:
        return f"{job_id}_pose_{pose_index:02d}_rep_{int(replica_index):02d}"
    return f"{job_id}_pose_{pose_index:02d}"


def select_top_poses(report: ValidationReport, top_n: int) -> list[tuple[int, float | None]]:
    """Sort poses by docking score ascending (more negative = better) and take top-n.

    Returns list of (pose_index, docking_score) preserving original 1-based indices. The docking
    score is OPTIONAL: a non-docked input (e.g. an AlphaFold-predicted complex) has no score, so
    None-scored poses sort last (by index) instead of breaking the comparison.
    """
    poses = [(p.index, p.docking_score) for p in report.poses]
    # None compares as "worst" (sorts after any real score); ties/all-None fall back to index order.
    poses.sort(key=lambda t: (t[1] is None, t[1] if t[1] is not None else 0.0, t[0]))
    return poses[: max(1, top_n)]


def _ligand_type(create: JobCreate, report: ValidationReport) -> str:
    if create.ligand_type and create.ligand_type in LigandType.ALL:
        return create.ligand_type
    if report.ligand_type_candidates:
        cand = report.ligand_type_candidates[0]
        if cand in LigandType.ALL:
            return cand
    return LigandType.UNKNOWN


def _input_type(report: ValidationReport) -> str:
    it = (report.input_type or "pdbqt").lower()
    return it if it in InputType.ALL else InputType.PDBQT


def create_job_and_subjobs(
    db: Session,
    *,
    user_id: int,
    create: JobCreate,
    report: ValidationReport,
    upload_meta: dict,
    settings: Settings,
) -> tuple[Job, list[SubJob]]:
    """Create the Job + top-n SubJobs (sorted by score), persist, write metadata.

    Does NOT enqueue; the caller enqueues after commit so subjobs exist in the DB
    before any worker picks them up.
    """
    md_len = resolve_md_length(create, settings.DEFAULT_MD_LENGTH_NS)
    chem_source = create.ligand_chem_source if create.ligand_chem_source in ChemSource.ALL else ChemSource.SDF
    priority = create.priority if create.priority in Priority.ALL else Priority.NORMAL
    top_poses = select_top_poses(report, int(create.top_n_poses))
    n_replicas = max(1, min(10, int(getattr(create, "n_replicas", 1) or 1)))

    # Serialize id allocation + insert + commit so two concurrent creators can never
    # compute or commit the same job id. Job creation is not a hot path, so a coarse
    # in-process lock is acceptable and keeps the id scheme contract-exact.
    with _id_lock:
        job_id = _next_job_id(db)
        name = create.name or upload_meta.get("name") or f"MD {job_id}"

        job = Job(
            id=job_id,
            user_id=user_id,
            name=name,
            input_type=_input_type(report),
            ligand_type=_ligand_type(create, report),
            status=JobStatus.QUEUED,
            md_length_ns=md_len,
            top_n_poses=int(create.top_n_poses),
            n_replicas=n_replicas,
            force_field=create.force_field or settings.PROTEIN_FORCE_FIELD,
            ligand_force_field=create.ligand_force_field or settings.LIGAND_FORCE_FIELD,
            ligand_chem_source=chem_source,
            water_model=create.water_model or settings.WATER_MODEL,
            salt_concentration=float(create.salt_concentration),
            temperature=float(create.temperature),
            pressure=float(create.pressure),
            box_type=create.box_type,
            priority=priority,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(job)

        subjobs: list[SubJob] = []
        # Fan out each top pose into n_replicas independent subjobs (different random velocity
        # seeds via NVT gen_seed=-1). Replica 1 reuses the canonical pose id/dir so single-replica
        # jobs are unchanged; replicas 2..N add a `_rep_RR` suffix and are aggregated to mean ± SEM.
        for pose_index, score in top_poses:
            for replica in range(1, n_replicas + 1):
                sj = SubJob(
                    id=subjob_id(job_id, pose_index, replica),
                    job_id=job_id,
                    pose_index=pose_index,
                    replica_index=replica,
                    # docking_score is optional (AlphaFold/no-dock inputs have none); store 0.0 as
                    # the neutral sentinel so the non-null column is satisfied without breaking.
                    docking_score=float(score) if score is not None else 0.0,
                    status=JobStatus.QUEUED,
                    progress=0.0,
                    completed_ns=0.0,
                    ns_per_day=0.0,
                    current_step="queued",
                )
                db.add(sj)
                subjobs.append(sj)

        # Stage storage (dirs + copied inputs + metadata.json) BEFORE commit so a
        # committed/queued job always has its artifact tree. If staging fails we roll
        # back the DB and remove any partial tree, leaving no orphan rows.
        db.flush()  # assign defaults without committing
        try:
            _stage_job_storage(job, subjobs, create, report, upload_meta, settings)
            db.commit()
        except Exception:
            db.rollback()
            storage.remove_job_storage(job_id)
            raise

    db.refresh(job)
    for sj in subjobs:
        db.refresh(sj)
    return job, subjobs


def _stage_job_storage(
    job: Job,
    subjobs: list[SubJob],
    create: JobCreate,
    report: ValidationReport,
    upload_meta: dict,
    settings: Settings,
) -> None:
    """Create the job artifact tree and copy original inputs; write metadata.json."""
    jdir = storage.job_dir(job.id)
    storage.ensure_dirs(
        jdir,
        jdir / "input" / "original",
        jdir / "input" / "processed",
        storage.summary_dir(job.id),
    )
    for sj in subjobs:
        pdir = storage.pose_dir(job.id, sj.pose_index, sj.replica_index)
        storage.ensure_dirs(
            pdir / "prep",
            pdir / "md",
            pdir / "analysis",
            pdir / "visualization",
            pdir / "logs",
        )

    # Copy the uploaded originals into input/original/ so the worker is self-contained.
    upload_id = create.upload_id
    src_dir = storage.upload_dir(upload_id)
    original = jdir / "input" / "original"
    for key in ("pose_file", "chemistry_file", "receptor_file"):
        fname = upload_meta.get(key)
        if fname:
            src = src_dir / fname
            if src.exists():
                (original / fname).write_bytes(src.read_bytes())

    metadata = {
        "job_id": job.id,
        "name": job.name,
        "user_id": job.user_id,
        "upload_id": upload_id,
        "input_type": job.input_type,
        "ligand_type": job.ligand_type,
        "ligand_chem_source": job.ligand_chem_source,
        "md_length_ns": job.md_length_ns,
        "md_preset": create.md_preset,
        "top_n_poses": job.top_n_poses,
        "n_replicas": job.n_replicas,
        "force_field": job.force_field,
        "ligand_force_field": job.ligand_force_field,
        "water_model": job.water_model,
        "box_type": job.box_type,
        "salt_concentration": job.salt_concentration,
        "temperature": job.temperature,
        "pressure": job.pressure,
        "priority": job.priority,
        "use_gpu": create.use_gpu,
        "compute_mmpbsa": create.compute_mmpbsa,  # opt-in MM-PBSA/GBSA binding ΔG (mmpbsa step)
        "hetatm_decisions": create.hetatm_decisions,
        "cif_options": create.cif_options,
        "smiles": upload_meta.get("smiles"),
        "engine": settings.resolved_md_engine(),
        "input_files": {
            "pose_file": upload_meta.get("pose_file"),
            "chemistry_file": upload_meta.get("chemistry_file"),
            "receptor_file": upload_meta.get("receptor_file"),
        },
        "subjobs": [
            {"id": sj.id, "pose_index": sj.pose_index, "replica_index": sj.replica_index,
             "docking_score": sj.docking_score}
            for sj in subjobs
        ],
        "validation_report": copy.deepcopy(report.model_dump()),
        "created_at": job.created_at.isoformat(),
    }
    storage.write_json(jdir / "metadata.json", metadata)


def cancel_job(db: Session, job: Job) -> Job:
    """Transition a job + its non-terminal subjobs to cancelled and free GPUs.

    Uses a compare-and-swap guard per subjob: only subjobs still in a non-terminal
    state are flipped, so a subjob the worker just completed is left as completed.
    A worker that reports progress for an already-cancelled subjob is ignored by the
    internal status handler (see ``apply_subjob_status``), preventing resurrection.
    """
    from . import gpu_manager

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Single transaction: cancel non-terminal subjobs, release their GPU slots, and cancel the
    # parent job, then commit once — all of it lands or none does.
    #
    # Atomic compare-and-swap: flip ONLY non-terminal subjobs to cancelled in a conditional
    # UPDATE so a subjob the worker completed between our read and write is not clobbered.
    subjob_ids = db.execute(select(SubJob.id).where(SubJob.job_id == job.id)).scalars().all()
    db.execute(
        update(SubJob)
        .where(SubJob.job_id == job.id, SubJob.status.notin_(JobStatus.TERMINAL_SET))
        .values(status=JobStatus.CANCELLED, current_step="cancelled", completed_at=now)
    )
    # Release each subjob's GPU slot via the slot-counted source of truth (decrements
    # running_count, not just clears the marker) WITHOUT committing, so it participates in this
    # transaction. Guarded against a concurrent worker release, so it can't double-decrement.
    for sid in subjob_ids:
        gpu_manager.release_gpu(db, sid, commit=False)
    job.status = JobStatus.CANCELLED
    job.completed_at = now
    db.commit()
    db.refresh(job)
    return job


def retry_job(db: Session, job: Job) -> tuple[Job, list[SubJob]]:
    """Requeue failed/cancelled subjobs. Returns (job, requeued_subjobs).

    Raises ValueError if nothing is retryable so the router can map it to 409.
    """
    from . import gpu_manager

    subjobs = db.execute(select(SubJob).where(SubJob.job_id == job.id)).scalars().all()
    to_requeue = [s for s in subjobs if s.status in (JobStatus.FAILED, JobStatus.CANCELLED)]
    if not to_requeue:
        raise ValueError("No failed or cancelled poses to retry.")
    for sj in to_requeue:
        # Release any GPU slot the subjob still holds BEFORE clearing its assignment, so
        # running_count is decremented (not leaked). Non-committing -> part of this transaction.
        gpu_manager.release_gpu(db, sj.id, commit=False)
        sj.status = JobStatus.QUEUED
        sj.current_step = "queued"
        sj.progress = 0.0
        sj.completed_ns = 0.0
        sj.ns_per_day = 0.0
        sj.error_message = None
        sj.assigned_gpu = None  # release_gpu already cleared it; explicit for clarity
        sj.started_at = None
        sj.completed_at = None
    job.status = JobStatus.QUEUED
    job.completed_at = None
    job.error_message = None
    db.commit()
    db.refresh(job)
    return job, to_requeue


def delete_job(db: Session, job: Job) -> None:
    """Release GPUs and remove subjobs, logs, the job row, and storage."""
    from ..models import JobLog  # local import to avoid cycle at module load

    subjobs = db.execute(select(SubJob).where(SubJob.job_id == job.id)).scalars().all()
    for sj in subjobs:
        gpu_manager.release_gpu(db, sj.id)
        db.delete(sj)
    db.query(JobLog).filter(JobLog.job_id == job.id).delete(synchronize_session=False)
    job_id = job.id
    db.delete(job)
    db.commit()
    storage.remove_job_storage(job_id)


def storage_estimate_gb(top_n: int, md_length_ns: int) -> float:
    """Rough per-job storage estimate for the UI (heuristic, clearly approximate)."""
    # ~0.4 GB per pose at 50 ns as a coarse linear heuristic.
    per_pose = 0.4 * (md_length_ns / 50.0)
    return round(per_pose * max(1, top_n), 2)


def find_pose_dir(job_id: str, pose_index: int, replica_index: int = 1) -> Path:
    return storage.pose_dir(job_id, pose_index, replica_index)


def replica_stats(values: list[float]) -> dict:
    """mean ± SEM (and std/min/max) across replicas. n = number of replicas, NOT frames.

    Uses the sample standard deviation (ddof=1); SEM = std / sqrt(n). A single replica has no
    spread (sem/std = 0); zero replicas yields all-None so the UI shows "—"."""
    vals = [float(v) for v in values if isinstance(v, (int, float))]
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "sem": None, "std": None, "min": None, "max": None}
    mean = sum(vals) / n
    if n == 1:
        return {"n": 1, "mean": round(mean, 3), "sem": 0.0, "std": 0.0,
                "min": round(mean, 3), "max": round(mean, 3)}
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    std = var ** 0.5
    return {"n": n, "mean": round(mean, 3), "sem": round(std / (n ** 0.5), 3),
            "std": round(std, 3), "min": round(min(vals), 3), "max": round(max(vals), 3)}


def pose_replica_aggregates(job: Job, subjobs: list[SubJob]) -> list[dict]:
    """Group a job's subjobs by pose and aggregate each pose's replicas to mean ± SEM of the
    binding score + pose occupancy, reading each replica's analysis/mmpbsa.json. Returns [] for
    single-replica jobs (nothing to aggregate)."""
    if int(getattr(job, "n_replicas", 1) or 1) <= 1:
        return []
    by_pose: dict[int, list[SubJob]] = {}
    for sj in subjobs:
        by_pose.setdefault(sj.pose_index, []).append(sj)
    out: list[dict] = []
    for pose_index in sorted(by_pose):
        reps = sorted(by_pose[pose_index], key=lambda s: s.replica_index)
        gbsa: list[float] = []
        pbsa: list[float] = []
        occ: list[float] = []
        per_rep: list[dict] = []
        for sj in reps:
            data = storage.read_json(
                storage.pose_dir(job.id, sj.pose_index, sj.replica_index) / "analysis" / "mmpbsa.json"
            ) or {}
            g = data.get("gbsa_dg_kcal_mol")
            p = data.get("pbsa_dg_kcal_mol")
            o = data.get("pose_occupancy")
            if isinstance(g, (int, float)):
                gbsa.append(float(g))
            if isinstance(p, (int, float)):
                pbsa.append(float(p))
            if isinstance(o, (int, float)):
                occ.append(float(o))
            per_rep.append({
                "replica_index": sj.replica_index, "subjob_id": sj.id, "status": sj.status,
                "gbsa_dg_kcal_mol": g, "pbsa_dg_kcal_mol": p, "pose_occupancy": o,
            })
        out.append({
            "pose_index": pose_index,
            "n_replicas": len(reps),
            "gbsa": replica_stats(gbsa),
            "pbsa": replica_stats(pbsa),
            "pose_occupancy": replica_stats(occ),
            "replicas": per_rep,
        })
    return out
