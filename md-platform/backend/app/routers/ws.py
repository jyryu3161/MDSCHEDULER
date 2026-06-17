"""WebSocket router (CONTRACT §5 Realtime).

SSE is the MVP primary; these WS routes send the same JSON payloads as frames so a
WS-based client can be used interchangeably. Auth uses the ``token`` query parameter
(WebSocket cannot carry an Authorization header reliably from browsers).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..database import session_scope
from ..models import Job, Role, SubJob, User
from ..realtime import bus, dashboard_topic, job_topic
from ..security import decode_access_token
from .dashboard import build_summary
from .gpus import gpu_manager
from .queue import build_queue_snapshot

router = APIRouter(prefix="/ws", tags=["ws"])


def _authenticate(token: str | None) -> tuple[int, str] | None:
    """Return (user_id, role) for a valid token, else None. Values are copied out
    of the session so callers never touch a detached ORM instance."""
    if not token:
        return None
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    try:
        payload = decode_access_token(token)
        uid = int(payload.get("uid"))
    except Exception:
        return None
    with session_scope() as s:
        user = s.get(User, uid)
        if user is None or not user.is_active:
            return None
        return (user.id, user.role)


def _dashboard_payload(viewer_id: int | None = None, is_admin: bool = False) -> dict:
    with session_scope() as s:
        summary = build_summary(s, viewer_id=viewer_id, is_admin=is_admin).model_dump()
        gpus = gpu_manager.list_gpus(s)
        gpu_payload = [
            {
                "gpu_id": g.gpu_id,
                "name": g.name,
                "status": g.status,
                "utilization": g.utilization,
                "memory_used": g.memory_used,
                "memory_total": g.memory_total,
                "temperature": g.temperature,
                "assigned_subjob_id": g.assigned_subjob_id,
            }
            for g in gpus
        ]
        queue = build_queue_snapshot(s, viewer_id=viewer_id, is_admin=is_admin).model_dump()
    return {"event": "dashboard", "summary": summary, "gpus": gpu_payload, "queue": queue}


def _job_payload(job_id: str) -> dict:
    with session_scope() as s:
        j = s.get(Job, job_id)
        if j is None:
            return {"event": "job", "job_id": job_id, "status": "deleted"}
        subs = s.query(SubJob).filter(SubJob.job_id == job_id).order_by(SubJob.pose_index).all()
        return {
            "event": "job",
            "job_id": j.id,
            "status": j.status,
            "name": j.name,
            "subjobs": [
                {
                    "id": x.id,
                    "pose_index": x.pose_index,
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


@router.websocket("/dashboard")
async def ws_dashboard(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    auth = _authenticate(token)
    if auth is None:
        await websocket.close(code=4401)
        return
    user_id, role = auth
    is_admin = role == Role.ADMIN
    await websocket.accept()
    q = await bus.subscribe(dashboard_topic())
    try:
        await websocket.send_json(_dashboard_payload(viewer_id=user_id, is_admin=is_admin))
        while True:
            try:
                await asyncio.wait_for(q.get(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            if websocket.application_state != WebSocketState.CONNECTED:
                break
            await websocket.send_json(_dashboard_payload(viewer_id=user_id, is_admin=is_admin))
    except WebSocketDisconnect:
        pass
    finally:
        await bus.unsubscribe(dashboard_topic(), q)


@router.websocket("/jobs/{job_id}")
async def ws_job(websocket: WebSocket, job_id: str, token: str | None = Query(default=None)) -> None:
    auth = _authenticate(token)
    if auth is None:
        await websocket.close(code=4401)
        return
    user_id, role = auth
    # Authorization: owner or admin.
    with session_scope() as s:
        job = s.get(Job, job_id)
        authorized = job is not None and (job.user_id == user_id or role == Role.ADMIN)
    if not authorized:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    topic = job_topic(job_id)
    q = await bus.subscribe(topic)
    try:
        await websocket.send_json(_job_payload(job_id))
        while True:
            try:
                await asyncio.wait_for(q.get(), timeout=3.0)
                if websocket.application_state != WebSocketState.CONNECTED:
                    break
                await websocket.send_json(_job_payload(job_id))
            except asyncio.TimeoutError:
                if websocket.application_state != WebSocketState.CONNECTED:
                    break
                await websocket.send_json(_job_payload(job_id))
    except WebSocketDisconnect:
        pass
    finally:
        await bus.unsubscribe(topic, q)
