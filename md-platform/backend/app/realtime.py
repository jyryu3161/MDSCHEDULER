"""In-process asyncio pub/sub event bus + SSE generators.

The bus is process-local: in ``QUEUE_BACKEND=local`` the worker runs in-process so
events flow directly; in ``rq`` mode the worker reports via the Internal API, whose
handlers publish onto this same bus. SSE generators additionally poll the DB so a
freshly connected client always receives a current snapshot even if it missed live
events (and so rq-mode multi-process gaps degrade to ~3s polling rather than silence).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator


class EventBus:
    """Topic-based asyncio fan-out. Subscribers receive dict payloads."""

    def __init__(self) -> None:
        self._topics: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, topic: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._topics.setdefault(topic, set()).add(q)
        return q

    async def unsubscribe(self, topic: str, q: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._topics.get(topic)
            if subs and q in subs:
                subs.discard(q)
                if not subs:
                    self._topics.pop(topic, None)

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            subs = list(self._topics.get(topic, ()))
        for q in subs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop oldest then enqueue newest to keep the stream live.
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass

    def publish_threadsafe(self, loop: asyncio.AbstractEventLoop | None, topic: str, payload: dict[str, Any]) -> None:
        """Publish from a non-async thread (e.g. the LocalExecutor worker thread)."""
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.publish(topic, payload), loop)
        except RuntimeError:
            pass


# Process-global bus + the running event loop captured at startup.
bus = EventBus()
_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


def get_main_loop() -> asyncio.AbstractEventLoop | None:
    return _main_loop


def publish_from_thread(topic: str, payload: dict[str, Any]) -> None:
    """Convenience wrapper used by synchronous DB reporter / background code."""
    bus.publish_threadsafe(_main_loop, topic, payload)


def dashboard_topic() -> str:
    return "dashboard"


def job_topic(job_id: str) -> str:
    return f"job:{job_id}"


async def sse_dashboard_stream(snapshot_fn) -> AsyncIterator[dict[str, str]]:
    """Yield dashboard SSE events ~every 3s plus any live published events.

    ``snapshot_fn`` is a zero-arg callable returning the full dashboard dict.
    """
    q = await bus.subscribe(dashboard_topic())
    try:
        # Immediate snapshot on connect.
        yield {"event": "dashboard", "data": json.dumps(snapshot_fn())}
        while True:
            try:
                await asyncio.wait_for(q.get(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            yield {"event": "dashboard", "data": json.dumps(snapshot_fn())}
    finally:
        await bus.unsubscribe(dashboard_topic(), q)


async def sse_job_stream(job_id: str, snapshot_fn) -> AsyncIterator[dict[str, str]]:
    """Yield per-job SSE events. ``snapshot_fn(job_id)`` returns the job dict."""
    topic = job_topic(job_id)
    q = await bus.subscribe(topic)
    try:
        yield {"event": "job", "data": json.dumps(snapshot_fn(job_id))}
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=3.0)
                yield {"event": "job", "data": json.dumps(payload)}
            except asyncio.TimeoutError:
                yield {"event": "job", "data": json.dumps(snapshot_fn(job_id))}
    finally:
        await bus.unsubscribe(topic, q)
