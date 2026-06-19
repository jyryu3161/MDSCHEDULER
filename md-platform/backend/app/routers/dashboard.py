"""Dashboard summary router (CONTRACT §5 Dashboard summary)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import GpuStatus, GpuStatusEnum, Job, JobStatus, Role, User
from ..schemas import DashboardSummary
from ..services import storage

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def build_summary(db: Session, viewer_id: int | None = None, is_admin: bool = False) -> DashboardSummary:
    # Job counts are scoped to the viewer for non-admins (so one user's dashboard doesn't expose
    # how many jobs others are running); admins (and unscoped internal callers) see global counts.
    # GPU + storage are shared infrastructure status and stay global for everyone.
    scope_uid = viewer_id if (viewer_id is not None and not is_admin) else None

    job_counts_q = select(Job.status, func.count()).select_from(Job)
    if scope_uid is not None:
        job_counts_q = job_counts_q.where(Job.user_id == scope_uid)
    job_counts = {status: int(count) for status, count in db.execute(job_counts_q.group_by(Job.status)).all()}
    total = sum(job_counts.values())
    running = sum(job_counts.get(s, 0) for s in JobStatus.RUNNING_SET)
    queued = sum(job_counts.get(s, 0) for s in (JobStatus.QUEUED, JobStatus.UPLOADED, JobStatus.VALIDATING))
    completed = job_counts.get(JobStatus.COMPLETED, 0)
    failed = job_counts.get(JobStatus.FAILED, 0)

    gpu_counts = {
        status: int(count)
        for status, count in db.execute(
            select(GpuStatus.status, func.count()).select_from(GpuStatus).group_by(GpuStatus.status)
        ).all()
    }
    gpus_available = gpu_counts.get(GpuStatusEnum.AVAILABLE, 0)
    gpus_busy = gpu_counts.get(GpuStatusEnum.BUSY, 0)

    used_gb, total_gb = storage.disk_usage_gb()
    return DashboardSummary(
        total_jobs=total,
        running_jobs=running,
        queued_jobs=queued,
        completed_jobs=completed,
        failed_jobs=failed,
        gpus_available=gpus_available,
        gpus_busy=gpus_busy,
        storage_used_gb=used_gb,
        storage_total_gb=total_gb,
    )


@router.get("/summary", response_model=DashboardSummary)
def dashboard_summary(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DashboardSummary:
    return build_summary(db, viewer_id=user.id, is_admin=(user.role == Role.ADMIN))
