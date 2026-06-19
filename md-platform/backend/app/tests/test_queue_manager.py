"""Queue manager enqueue contracts."""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="mdplatform_queue_manager_")
os.environ.setdefault("STORAGE_ROOT", _TMP)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/test.db")
os.environ.setdefault("QUEUE_BACKEND", "local")
os.environ.setdefault("JWT_SECRET", "test-secret")


class _FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def enqueue(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def test_rq_md_enqueue_passes_runner_settings(monkeypatch):
    from app.services.queue_manager import QueueManager

    qm = object.__new__(QueueManager)
    qm._backend = "rq"
    qm._rq_queue = _FakeQueue()
    qm._design_queue = None
    monkeypatch.setattr(qm, "_runner_settings", lambda: {"GEMINI_API_KEY": "db-key"}, raising=False)

    qm.enqueue("job_1_pose_01")

    args, kwargs = qm._rq_queue.calls[0]
    assert args == ("mdworker.tasks.run_subjob_task", "job_1_pose_01", {"GEMINI_API_KEY": "db-key"})
    assert kwargs["job_timeout"] == "24h"


def test_rq_design_enqueue_goes_to_design_worker_queue(monkeypatch):
    from app.services import design_service
    from app.services.queue_manager import QueueManager

    qm = object.__new__(QueueManager)
    qm._backend = "rq"
    qm._rq_queue = _FakeQueue()
    qm._design_queue = _FakeQueue()
    monkeypatch.setattr(
        design_service,
        "design_task_payload",
        lambda design_id: ({"strategy": "ga"}, {"GEMINI_API_KEY": "db-key"}),
    )

    qm.enqueue_design("design_1")

    assert qm._rq_queue.calls == []
    args, kwargs = qm._design_queue.calls[0]
    assert args == (
        "mdworker.tasks.run_design_task",
        "design_1",
        {"strategy": "ga"},
        {"GEMINI_API_KEY": "db-key"},
    )
    assert kwargs["job_timeout"] == "72h"
