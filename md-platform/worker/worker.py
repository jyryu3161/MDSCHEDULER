"""RQ worker bootstrap (containerized GPU worker entry point).

Each `worker-gpu-N` container runs this. It pins the worker to its GPU via
``CUDA_VISIBLE_DEVICES`` / ``WORKER_GPU_ID`` (set by docker-compose), connects to Redis, and
processes the default queue, invoking ``mdworker.tasks.run_subjob_task`` per job. The actual
GPU lock is allocated by the backend (one subjob per GPU) via the Reporter; this process just
consumes the queue.

Local development uses the backend's in-process LocalExecutor instead (QUEUE_BACKEND=local),
so this entry point is not needed without Docker/Redis.
"""

from __future__ import annotations

import os
import sys

from mdworker.config import load_settings


def _redact_url(url: str) -> str:
    """Hide any user:password@ credentials before logging a connection URL."""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            netloc = f"***@{host}" if host else "***"
            return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
        return url
    except Exception:  # noqa: BLE001
        return "<redis-url>"


def main() -> int:
    settings = load_settings()
    try:
        from redis import Redis
        from rq import Queue, Worker
    except ImportError as exc:  # pragma: no cover - rq optional extra
        print(f"[mdworker] rq/redis not installed ({exc}); install mdworker[rq].", file=sys.stderr)
        return 1

    gpu = os.environ.get("WORKER_GPU_ID", "?")
    # Must match the queue the backend enqueues to (services/queue_manager.py -> Queue("md")).
    queue_name = os.environ.get("RQ_QUEUE", "md")
    conn = Redis.from_url(settings.redis_url)
    print(f"[mdworker] worker starting: GPU={gpu} "
          f"CUDA_VISIBLE_DEVICES={os.environ.get('CUDA_VISIBLE_DEVICES','')} "
          f"queue={queue_name} redis={_redact_url(settings.redis_url)} engine={settings.resolved_engine}",
          flush=True)
    worker = Worker([Queue(queue_name, connection=conn)], connection=conn)
    worker.work(with_scheduler=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
