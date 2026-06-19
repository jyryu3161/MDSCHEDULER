"""Server-Sent Events router (CONTRACT §5 Realtime).

EventSource cannot set Authorization headers, so these endpoints accept the JWT
either via the standard ``Authorization: Bearer`` header OR a ``token`` query
parameter (the documented SSE fallback). Snapshots are computed by querying the DB
each tick so a fresh connection always sees current state and rq-mode multi-process
gaps degrade to ~3s polling.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.orm import Session

from ..database import get_db, session_scope
from ..models import Job, Role, User
from ..realtime import sse_dashboard_stream, sse_job_stream
from ..security import decode_access_token
from .dashboard import build_summary
from .gpus import gpu_manager
from .queue import build_queue_snapshot

router = APIRouter(prefix="/events", tags=["events"])


def _user_from_token(token: str | None, db: Session) -> User:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token.")
    # Allow "Bearer <jwt>" or a bare jwt.
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    try:
        payload = decode_access_token(token)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token.")
    uid = payload.get("uid")
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token.")
    user = db.get(User, uid_int)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive.")
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change required before using the API.",
        )
    return user


def _resolve_token(request: Request, token_q: str | None) -> str | None:
    if token_q:
        return token_q
    auth = request.headers.get("Authorization")
    return auth


@router.get("/dashboard")
async def dashboard_events(
    request: Request,
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    viewer = _user_from_token(_resolve_token(request, token), db)
    # Capture as primitives so the streaming closure doesn't touch a detached ORM object after the
    # request session closes. Job counts + queue are scoped to this viewer (admins see global).
    viewer_id = viewer.id
    is_admin = viewer.role == Role.ADMIN

    def snapshot() -> dict:
        # Each tick uses its own short-lived session (the request session is closed
        # once the generator starts streaming).
        with session_scope() as s:
            summary = build_summary(s, viewer_id=viewer_id, is_admin=is_admin).model_dump()
            gpus = [g_.__dict__ for g_ in gpu_manager.list_gpus(s)]
            gpu_payload = [
                {
                    "gpu_id": g["gpu_id"],
                    "name": g["name"],
                    "status": g["status"],
                    "utilization": g["utilization"],
                    "memory_used": g["memory_used"],
                    "memory_total": g["memory_total"],
                    "temperature": g["temperature"],
                    "assigned_subjob_id": g["assigned_subjob_id"],
                }
                for g in gpus
            ]
            queue = build_queue_snapshot(s, viewer_id=viewer_id, is_admin=is_admin).model_dump()
        return {"summary": summary, "gpus": gpu_payload, "queue": queue}

    return EventSourceResponse(sse_dashboard_stream(snapshot))


@router.get("/jobs/{job_id}")
async def job_events(
    job_id: str,
    request: Request,
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    user = _user_from_token(_resolve_token(request, token), db)
    job = db.get(Job, job_id)
    if job is None or (job.user_id != user.id and user.role != Role.ADMIN):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    def snapshot(jid: str) -> dict:
        with session_scope() as s:
            j = s.get(Job, jid)
            if j is None:
                return {"job_id": jid, "status": "deleted"}
            from ..models import SubJob

            subs = s.query(SubJob).filter(SubJob.job_id == jid).order_by(
                SubJob.pose_index, SubJob.replica_index).all()
            return {
                "job_id": j.id,
                "status": j.status,
                "name": j.name,
                "subjobs": [
                    {
                        "id": x.id,
                        "pose_index": x.pose_index,
                        "replica_index": x.replica_index,
                        "status": x.status,
                        "progress": x.progress,
                        "current_step": x.current_step,
                        "completed_ns": x.completed_ns,
                        "ns_per_day": x.ns_per_day,
                        "assigned_gpu": x.assigned_gpu,
                        "error_message": x.error_message,
                    }
                    for x in subs
                ],
            }

    return EventSourceResponse(sse_job_stream(job_id, snapshot))
