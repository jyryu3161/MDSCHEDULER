"""Internal worker -> backend router (CONTRACT §5 Internal).

Guarded by the shared ``X-Internal-Token`` header. Used by the RQ-mode worker's
HttpReporter. State transitions go through ``apply_subjob_status`` which refuses to
resurrect a subjob the backend has already moved to a terminal state (e.g. a user
cancelled the job while the worker was mid-step), preventing the worker from
clobbering an authoritative cancel.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import realtime
from ..database import get_db
from ..deps import verify_internal_token
from ..models import Job, JobLog, JobStatus, SubJob, utcnow
from ..schemas import (
    InternalGpuAssign,
    InternalGpuReleaseRequest,
    InternalGpuRequest,
    InternalGpuRequestResponse,
    InternalJobStatus,
    InternalLog,
    InternalSubjobStatus,
    OkResponse,
)
from ..services import gpu_manager

router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(verify_internal_token)])


def _publish_job(db: Session, job_id: str) -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    subs = db.query(SubJob).filter(SubJob.job_id == job_id).order_by(SubJob.pose_index).all()
    realtime.publish_from_thread(
        realtime.job_topic(job_id),
        {
            "job_id": job_id,
            "status": job.status,
            "name": job.name,
            "subjobs": [
                {
                    "id": s.id,
                    "pose_index": s.pose_index,
                    "status": s.status,
                    "progress": s.progress,
                    "current_step": s.current_step,
                    "completed_ns": s.completed_ns,
                    "ns_per_day": s.ns_per_day,
                    "assigned_gpu": s.assigned_gpu,
                    "error_message": s.error_message,
                }
                for s in subs
            ],
        },
    )
    realtime.publish_from_thread(realtime.dashboard_topic(), {"trigger": "job_update", "job_id": job_id})


@router.post("/subjobs/{subjob_id}/status", response_model=OkResponse)
def update_subjob_status(
    subjob_id: str,
    body: InternalSubjobStatus,
    db: Session = Depends(get_db),
) -> OkResponse:
    sub = db.get(SubJob, subjob_id)
    if sub is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SubJob not found.")

    # Resurrection guard: once a subjob is terminal (e.g. cancelled by the user),
    # the backend decision is authoritative. Ignore ANY incoming status change —
    # including a different terminal status like completed/failed from a worker that
    # hadn't yet observed the cancel. Metric/log-style fields below are still
    # accepted so late telemetry isn't lost.
    is_terminal = sub.status in JobStatus.TERMINAL_SET
    if body.status is not None and not is_terminal:
        sub.status = body.status
    if body.current_step is not None:
        sub.current_step = body.current_step
    if body.progress is not None:
        sub.progress = float(body.progress)
    if body.completed_ns is not None:
        sub.completed_ns = float(body.completed_ns)
    if body.ns_per_day is not None:
        sub.ns_per_day = float(body.ns_per_day)
    if body.assigned_gpu is not None:
        sub.assigned_gpu = body.assigned_gpu
    if body.error_message is not None:
        sub.error_message = body.error_message
    if body.started_at is not None:
        sub.started_at = _naive(body.started_at)
    if body.completed_at is not None:
        sub.completed_at = _naive(body.completed_at)
    if body.result_path is not None:
        sub.result_path = body.result_path

    job_id = sub.job_id
    db.commit()
    _maybe_finalize_job(db, job_id)
    _publish_job(db, job_id)
    return OkResponse(ok=True)


@router.get("/subjobs/{subjob_id}/cancelled")
def subjob_cancelled(subjob_id: str, db: Session = Depends(get_db)) -> dict:
    """Cancel signal poll for the rq-mode worker (HttpReporter.is_cancelled).

    Returns ``{cancelled: bool}`` — true once the subjob or its parent job is cancelled, so the
    worker can terminate the in-flight gmx subprocess (force-stop a running MD job).
    """
    sub = db.get(SubJob, subjob_id)
    if sub is None:
        return {"cancelled": False}
    if sub.status == JobStatus.CANCELLED:
        return {"cancelled": True}
    job = db.get(Job, sub.job_id)
    return {"cancelled": bool(job is not None and job.status == JobStatus.CANCELLED)}


@router.post("/jobs/{job_id}/status", response_model=OkResponse)
def update_job_status(
    job_id: str,
    body: InternalJobStatus,
    db: Session = Depends(get_db),
) -> OkResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    # Do not resurrect a cancelled job from a stale worker report.
    if job.status == JobStatus.CANCELLED and body.status and body.status != JobStatus.CANCELLED:
        body.status = None

    if body.status is not None:
        job.status = body.status
    if body.result_path is not None:
        job.result_path = body.result_path
    if body.error_message is not None:
        job.error_message = body.error_message
    if body.started_at is not None:
        job.started_at = _naive(body.started_at)
    if body.completed_at is not None:
        job.completed_at = _naive(body.completed_at)
    db.commit()
    _publish_job(db, job_id)
    return OkResponse(ok=True)


@router.post("/logs", response_model=OkResponse)
def add_log(body: InternalLog, db: Session = Depends(get_db)) -> OkResponse:
    entry = JobLog(
        job_id=body.job_id,
        subjob_id=body.subjob_id,
        level=body.level,
        step=body.step,
        message=body.message,
        created_at=utcnow(),
    )
    db.add(entry)
    db.commit()
    realtime.publish_from_thread(
        realtime.job_topic(body.job_id),
        {
            "job_id": body.job_id,
            "subjob_id": body.subjob_id,
            "log": {"level": body.level, "step": body.step, "message": body.message},
        },
    )
    return OkResponse(ok=True)


@router.post("/gpus/{gpu_id}/assign", response_model=OkResponse)
def assign_gpu(
    gpu_id: int,
    body: InternalGpuAssign,
    db: Session = Depends(get_db),
) -> OkResponse:
    row = gpu_manager.set_gpu_state(db, gpu_id, status=body.status, subjob_id=body.subjob_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GPU not found.")
    realtime.publish_from_thread(realtime.dashboard_topic(), {"trigger": "gpu_update", "gpu_id": gpu_id})
    return OkResponse(ok=True)


@router.post("/gpus/request", response_model=InternalGpuRequestResponse)
def request_gpu(
    body: InternalGpuRequest,
    db: Session = Depends(get_db),
) -> InternalGpuRequestResponse:
    gpu_id = gpu_manager.request_gpu(db, body.subjob_id)
    if gpu_id is not None:
        realtime.publish_from_thread(realtime.dashboard_topic(), {"trigger": "gpu_update", "gpu_id": gpu_id})
    return InternalGpuRequestResponse(gpu_id=gpu_id)


@router.post("/gpus/release", response_model=OkResponse)
def release_gpu(
    body: InternalGpuReleaseRequest,
    db: Session = Depends(get_db),
) -> OkResponse:
    gpu_manager.release_gpu(db, body.subjob_id)
    realtime.publish_from_thread(realtime.dashboard_topic(), {"trigger": "gpu_release", "subjob_id": body.subjob_id})
    return OkResponse(ok=True)


def _maybe_finalize_job(db: Session, job_id: str) -> None:
    """Set the job's terminal status once all its subjobs are terminal.

    Mirrors the local executor's finalization so rq-mode jobs also flip to a final
    state. A job with at least one completed pose is reported completed (partial
    success still yields downloadable results); only an all-failed/cancelled set
    becomes failed/cancelled.
    """
    job = db.get(Job, job_id)
    if job is None or job.status == JobStatus.CANCELLED:
        return
    subs = db.query(SubJob).filter(SubJob.job_id == job_id).all()
    if not subs:
        return
    if any(s.status not in JobStatus.TERMINAL_SET for s in subs):
        return
    if any(s.status == JobStatus.COMPLETED for s in subs):
        job.status = JobStatus.COMPLETED
    elif all(s.status == JobStatus.CANCELLED for s in subs):
        job.status = JobStatus.CANCELLED
    else:
        job.status = JobStatus.FAILED
    if job.completed_at is None:
        job.completed_at = utcnow()
    # Advertise the downloadable result location only when at least one pose completed,
    # matching the local-executor finalization (db_reporter) so rq-mode jobs are consistent.
    if job.status == JobStatus.COMPLETED and job.result_path is None:
        from ..services.storage import job_dir
        job.result_path = str(job_dir(job_id))
    db.commit()


def _naive(dt: datetime) -> datetime:
    """Normalize an aware datetime to naive UTC for storage consistency."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
