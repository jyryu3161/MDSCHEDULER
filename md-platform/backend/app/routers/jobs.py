"""Jobs router (CONTRACT §5 Jobs)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..database import get_db
from ..deps import get_current_user
from ..models import Job, JobLog, Role, SubJob, User
from ..schemas import (
    JobCreate,
    JobDetail,
    JobLogOut,
    JobOut,
    OkResponse,
    SubJobOut,
)
from ..services import jobs_service, storage, validation
from ..services.queue_manager import get_queue_manager

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _owned_or_admin(job: Job, user: User) -> None:
    if job.user_id != user.id and user.role != Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")


def _reject(code: str, message: str, report) -> HTTPException:
    return HTTPException(
        # 422 per CONTRACT §7. Literal avoids the starlette constant-rename
        # deprecation warning while remaining exactly the required status code.
        status_code=422,
        detail={"code": code, "message": message, "report": report.model_dump() if report else None},
    )


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def create_job(
    body: JobCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> JobOut:
    # Preset/permission rule (CONTRACT §6): custom md length is admin-only.
    if body.md_preset == "custom" and user.role != Role.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Custom MD length requires administrator privileges. Use a preset (quick/standard/extended).",
        )

    # Locate the upload manifest.
    upload_dir = storage.upload_dir(body.upload_id)
    upload_meta = storage.read_json(upload_dir / "meta.json")
    if not upload_meta:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found.")
    if upload_meta.get("user_id") != user.id and user.role != Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found.")

    # Re-validate at creation time (authoritative gate).
    pose_name = upload_meta.get("pose_file")
    chem_name = upload_meta.get("chemistry_file")
    rec_name = upload_meta.get("receptor_file")
    report = validation.run_validation(
        pose_file=(upload_dir / pose_name) if pose_name else None,
        chemistry_file=(upload_dir / chem_name) if chem_name else None,
        receptor_file=(upload_dir / rec_name) if rec_name else None,
        smiles=upload_meta.get("smiles"),
        settings=settings,
    )

    ok, code, message = validation.enforce_hard_rules(report, settings=settings)
    if not ok:
        raise _reject(code or "VALIDATION_FAILED", message or "Input validation failed.", report)

    job, subjobs = jobs_service.create_job_and_subjobs(
        db,
        user_id=user.id,
        create=body,
        report=report,
        upload_meta=upload_meta,
        settings=settings,
    )

    # Enqueue each subjob after the rows + storage are committed.
    qm = get_queue_manager()
    for sj in subjobs:
        qm.enqueue(sj.id)

    return JobOut.model_validate(job)


@router.get("", response_model=list[JobOut])
def list_jobs(
    mine: bool = Query(default=True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[JobOut]:
    stmt = select(Job).order_by(Job.created_at.desc())
    if mine or user.role != Role.ADMIN:
        # Non-admins always see only their own jobs regardless of mine flag.
        stmt = stmt.where(Job.user_id == user.id)
    rows = db.execute(stmt).scalars().all()
    return [JobOut.model_validate(j) for j in rows]


@router.get("/{job_id}", response_model=JobDetail)
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JobDetail:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    _owned_or_admin(job, user)

    subjobs = (
        db.execute(select(SubJob).where(SubJob.job_id == job_id).order_by(SubJob.pose_index))
        .scalars()
        .all()
    )
    logs = (
        db.execute(
            select(JobLog).where(JobLog.job_id == job_id).order_by(JobLog.created_at.desc()).limit(200)
        )
        .scalars()
        .all()
    )
    return JobDetail(
        job=JobOut.model_validate(job),
        subjobs=[SubJobOut.model_validate(s) for s in subjobs],
        logs=[JobLogOut.model_validate(l) for l in reversed(logs)],
    )


@router.post("/{job_id}/cancel", response_model=JobOut)
def cancel_job(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JobOut:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    _owned_or_admin(job, user)
    job = jobs_service.cancel_job(db, job)
    return JobOut.model_validate(job)


@router.post("/{job_id}/retry", response_model=JobOut)
def retry_job(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JobOut:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    _owned_or_admin(job, user)
    try:
        job, to_requeue = jobs_service.retry_job(db, job)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    qm = get_queue_manager()
    for sj in to_requeue:
        qm.enqueue(sj.id)
    return JobOut.model_validate(job)


@router.delete("/{job_id}", response_model=OkResponse)
def delete_job(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    _owned_or_admin(job, user)
    jobs_service.delete_job(db, job)
    return OkResponse(ok=True)
