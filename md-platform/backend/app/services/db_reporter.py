"""DbReporter — backend-side implementation of the CONTRACT Reporter Protocol.

Used by the in-process LocalExecutor when ``QUEUE_BACKEND=local``. Structurally
matches the ``mdworker`` Reporter Protocol (CONTRACT §5):

    set_subjob_status(subjob_id, *, status, current_step, progress, completed_ns,
        ns_per_day, assigned_gpu, error_message, started_at, completed_at, result_path)
    set_job_status(job_id, *, status, result_path, error_message, started_at, completed_at)
    log(job_id, subjob_id, level, step, message)
    request_gpu(subjob_id) -> int | None
    release_gpu(subjob_id) -> None

It does NOT import ``mdworker``; the Protocol is structural, so duck-typing suffices
and the backend stays importable even when the worker package is absent. Each write
opens a short-lived session via the provided session factory (thread-safe for the
ThreadPoolExecutor) and publishes a realtime event.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import realtime
from ..models import Job, JobLog, JobStatus, SubJob, utcnow
from . import gpu_manager


def _as_dt(value):
    """Coerce a timestamp to a naive datetime for the DateTime columns.

    The worker runner emits ISO-8601 STRINGS for started_at/completed_at (JSON-friendly for
    the HTTP reporter path, where pydantic parses them). The in-process DbReporter writes
    directly to the DB, so strings must be parsed here; datetimes pass through; tz-aware
    datetimes are normalized to naive-UTC to match the column convention.
    """
    if value is None or isinstance(value, str) and not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is not None:
        from datetime import timezone
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class DbReporter:
    """Writes worker progress directly to the DB (in-process executor seam)."""

    def __init__(self, session_factory: Callable[[], Session]):
        self._session_factory = session_factory

    # ---- helpers ---------------------------------------------------------

    def _publish_job(self, db: Session, job_id: str) -> None:
        job = db.get(Job, job_id)
        if job is None:
            return
        subjobs = db.query(SubJob).filter(SubJob.job_id == job_id).order_by(SubJob.pose_index).all()
        payload = {
            "job_id": job.id,
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
                for s in subjobs
            ],
        }
        realtime.publish_from_thread(realtime.job_topic(job_id), payload)
        realtime.publish_from_thread(realtime.dashboard_topic(), {"trigger": "job_update", "job_id": job_id})

    def _maybe_finalize_job(self, db: Session, job_id: str) -> None:
        """If every subjob of ``job_id`` is terminal, set the parent job's terminal status.

        completed when all subjobs completed; completed (with downloadable results) when at
        least one completed; failed when none completed. Commits on the SAME session.
        """
        siblings = db.query(SubJob).filter(SubJob.job_id == job_id).all()
        if not siblings or any(s.status not in JobStatus.TERMINAL_SET for s in siblings):
            return
        job = db.get(Job, job_id)
        if job is None or job.status in JobStatus.TERMINAL_SET:
            return
        if all(s.status == JobStatus.COMPLETED for s in siblings):
            job.status = JobStatus.COMPLETED
        elif any(s.status == JobStatus.COMPLETED for s in siblings):
            job.status = JobStatus.COMPLETED  # partial success: results still downloadable
        elif all(s.status == JobStatus.CANCELLED for s in siblings):
            job.status = JobStatus.CANCELLED
        else:
            job.status = JobStatus.FAILED
        job.completed_at = utcnow()
        # Only advertise a downloadable result location when at least one pose completed.
        if job.status == JobStatus.COMPLETED and job.result_path is None:
            from .storage import job_dir
            job.result_path = str(job_dir(job_id))
        db.commit()

    # ---- Reporter Protocol ----------------------------------------------

    def set_subjob_status(
        self,
        subjob_id: str,
        *,
        status: str | None = None,
        current_step: str | None = None,
        progress: float | None = None,
        completed_ns: float | None = None,
        ns_per_day: float | None = None,
        assigned_gpu: int | None = None,
        error_message: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        result_path: str | None = None,
    ) -> None:
        db = self._session_factory()
        try:
            sub = db.get(SubJob, subjob_id)
            if sub is None:
                return
            job_id = sub.job_id
            # Telemetry/metric fields are always applied (even on an already-terminal subjob) so
            # late worker reports aren't lost.
            if current_step is not None:
                sub.current_step = current_step
            if progress is not None:
                sub.progress = float(progress)
            if completed_ns is not None:
                sub.completed_ns = float(completed_ns)
            if ns_per_day is not None:
                sub.ns_per_day = float(ns_per_day)
            if assigned_gpu is not None:
                sub.assigned_gpu = assigned_gpu
            if error_message is not None:
                sub.error_message = error_message
            if started_at is not None:
                sub.started_at = _as_dt(started_at)
            if completed_at is not None:
                sub.completed_at = _as_dt(completed_at)
            if result_path is not None:
                sub.result_path = result_path
            # Resurrection guard (mirrors routers/internal.py): once a subjob is terminal — e.g.
            # cancelled by the user — that decision is authoritative. Apply the status as a guarded
            # compare-and-swap (UPDATE ... WHERE status NOT IN terminal), matching the cancel
            # path's CAS, so a worker that read the row as non-terminal a moment ago can't clobber
            # a concurrent cancel.
            if status is not None:
                db.execute(
                    update(SubJob)
                    .where(SubJob.id == subjob_id, SubJob.status.notin_(JobStatus.TERMINAL_SET))
                    .values(status=status)
                )
            db.commit()
            db.refresh(sub)
            # When a subjob reaches a terminal state, finalize the parent job if ALL its subjobs
            # are now terminal. Keyed on the EFFECTIVE (post-CAS) status, so a blocked resurrection
            # doesn't trigger a spurious re-finalize, and a genuine terminal transition still does.
            # Single finalization point for both the in-process executor and the rq path.
            if sub.status in JobStatus.TERMINAL_SET:
                self._maybe_finalize_job(db, job_id)
            self._publish_job(db, job_id)
        finally:
            db.close()

    def set_job_status(
        self,
        job_id: str,
        *,
        status: str | None = None,
        result_path: str | None = None,
        error_message: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        db = self._session_factory()
        try:
            job = db.get(Job, job_id)
            if job is None:
                return
            if status is not None:
                job.status = status
            if result_path is not None:
                job.result_path = result_path
            if error_message is not None:
                job.error_message = error_message
            if started_at is not None:
                job.started_at = _as_dt(started_at)
            if completed_at is not None:
                job.completed_at = _as_dt(completed_at)
            db.commit()
            self._publish_job(db, job_id)
        finally:
            db.close()

    def log(self, job_id: str, subjob_id: str | None, level: str, step: str, message: str) -> None:
        db = self._session_factory()
        try:
            entry = JobLog(
                job_id=job_id,
                subjob_id=subjob_id,
                level=level or "info",
                step=step or "",
                message=message or "",
                created_at=utcnow(),
            )
            db.add(entry)
            db.commit()
            realtime.publish_from_thread(
                realtime.job_topic(job_id),
                {"job_id": job_id, "subjob_id": subjob_id, "log": {"level": level, "step": step, "message": message}},
            )
        finally:
            db.close()

    def request_gpu(self, subjob_id: str) -> int | None:
        db = self._session_factory()
        try:
            return gpu_manager.request_gpu(db, subjob_id)
        finally:
            db.close()

    def release_gpu(self, subjob_id: str) -> None:
        db = self._session_factory()
        try:
            gpu_manager.release_gpu(db, subjob_id)
        finally:
            db.close()

    def is_cancelled(self, subjob_id: str) -> bool:
        """True once the subjob (or its parent job) has been cancelled, so the in-process
        runner can abort and kill any in-flight subprocess."""
        db = self._session_factory()
        try:
            sub = db.get(SubJob, subjob_id)
            if sub is None:
                return False
            if sub.status == JobStatus.CANCELLED:
                return True
            job = db.get(Job, sub.job_id)
            return bool(job is not None and job.status == JobStatus.CANCELLED)
        finally:
            db.close()
