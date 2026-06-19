"""Queue router (CONTRACT §5 Queue)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import Job, JobStatus, Priority, Role, SubJob, User
from ..schemas import JobOut, PriorityUpdate, QueueItem, QueueResponse

router = APIRouter(prefix="/queue", tags=["queue"])


def build_queue_snapshot(db: Session, viewer_id: int | None = None, is_admin: bool = False) -> QueueResponse:
    """Assemble the queue snapshot: pending (queued) and running subjobs.

    Scoped to the viewer's own jobs for non-admins (so the queue doesn't expose other users' job
    names / usernames); admins (and unscoped internal callers) see the whole queue. Ordering
    mirrors scheduling intent: queued items by job priority then creation time then pose index;
    running items by assigned GPU. ETA is a rough estimate from measured ns/day when available.
    """
    scope_uid = viewer_id if (viewer_id is not None and not is_admin) else None
    q = (
        select(SubJob, Job, User)
        .join(Job, SubJob.job_id == Job.id)
        .join(User, Job.user_id == User.id)
        .where(SubJob.status.notin_(JobStatus.TERMINAL_SET))
    )
    if scope_uid is not None:
        q = q.where(Job.user_id == scope_uid)
    rows = db.execute(q).all()

    pending: list[tuple] = []
    running: list[tuple] = []
    for sj, job, user in rows:
        if sj.status == JobStatus.QUEUED:
            pending.append((sj, job, user))
        else:
            running.append((sj, job, user))

    pending.sort(key=lambda t: (
        Priority.ORDER.get(t[1].priority, 1), t[1].created_at, t[0].pose_index, t[0].replica_index))
    running.sort(key=lambda t: (
        t[0].assigned_gpu if t[0].assigned_gpu is not None else 1 << 30, t[0].pose_index, t[0].replica_index))

    def to_item(sj: SubJob, job: Job, user: User, position: int | None) -> QueueItem:
        return QueueItem(
            job_id=job.id,
            subjob_id=sj.id,
            job_name=job.name,
            user=user.username,
            pose_index=sj.pose_index,
            replica_index=sj.replica_index,
            status=sj.status,
            queue_position=position,
            assigned_gpu=sj.assigned_gpu,
            progress=sj.progress,
            completed_ns=sj.completed_ns,
            md_length_ns=job.md_length_ns,
            ns_per_day=sj.ns_per_day,
            rough_eta_seconds=_eta_seconds(sj, job),
        )

    items = [to_item(sj, job, user, i + 1) for i, (sj, job, user) in enumerate(pending)]
    running_items = [to_item(sj, job, user, None) for (sj, job, user) in running]
    return QueueResponse(items=items, running=running_items)


def _eta_seconds(sj: SubJob, job: Job) -> float | None:
    """Remaining seconds = remaining_ns / (ns_per_day / 86400). None if unknown."""
    if sj.ns_per_day and sj.ns_per_day > 0:
        remaining_ns = max(0.0, float(job.md_length_ns) - float(sj.completed_ns))
        ns_per_second = sj.ns_per_day / 86400.0
        if ns_per_second > 0:
            return round(remaining_ns / ns_per_second, 1)
    return None


@router.get("", response_model=QueueResponse)
def get_queue(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> QueueResponse:
    return build_queue_snapshot(db, viewer_id=user.id, is_admin=(user.role == Role.ADMIN))


@router.post("/{job_id}/priority", response_model=JobOut)
def set_priority(
    job_id: str,
    body: PriorityUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> JobOut:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    job.priority = body.priority
    db.commit()
    db.refresh(job)
    return JobOut.model_validate(job)
