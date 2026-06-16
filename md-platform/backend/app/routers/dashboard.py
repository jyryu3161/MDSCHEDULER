"""Dashboard summary router (CONTRACT §5 Dashboard summary)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import GpuStatus, GpuStatusEnum, Job, JobStatus, User
from ..schemas import DashboardSummary
from ..services import storage

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def build_summary(db: Session) -> DashboardSummary:
    def count_jobs(*statuses: str) -> int:
        return int(
            db.execute(
                select(func.count()).select_from(Job).where(Job.status.in_(statuses))
            ).scalar_one()
        )

    total = int(db.execute(select(func.count()).select_from(Job)).scalar_one())
    running = count_jobs(*JobStatus.RUNNING_SET)
    queued = count_jobs(JobStatus.QUEUED, JobStatus.UPLOADED, JobStatus.VALIDATING)
    completed = count_jobs(JobStatus.COMPLETED)
    failed = count_jobs(JobStatus.FAILED)

    gpus_available = int(
        db.execute(
            select(func.count()).select_from(GpuStatus).where(GpuStatus.status == GpuStatusEnum.AVAILABLE)
        ).scalar_one()
    )
    gpus_busy = int(
        db.execute(
            select(func.count()).select_from(GpuStatus).where(GpuStatus.status == GpuStatusEnum.BUSY)
        ).scalar_one()
    )

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
    _user: User = Depends(get_current_user),
) -> DashboardSummary:
    return build_summary(db)
