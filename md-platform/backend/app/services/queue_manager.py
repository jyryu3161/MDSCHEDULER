"""Queue manager — enqueue subjobs to RQ or an in-process ThreadPoolExecutor.

CONTRACT §5 enqueue contract:
  - rq mode    : rq.Queue.enqueue('mdworker.tasks.run_subjob_task', subjob_id)
  - local mode : ThreadPoolExecutor(max_workers=#GPUs) calling
                 mdworker.pipeline.runner.run_subjob(subjob_id,
                     reporter=DbReporter(SessionLocal), settings=...)
  - auto       : rq if redis ping ok else local

The worker package (``mdworker``) is import-guarded: in local mode the runner is
imported lazily inside the worker thread so the backend itself starts even if the
worker (or rdkit/gromacs) is missing; the subjob is then marked failed with a clear
message rather than taking down the process.
"""

from __future__ import annotations

import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..config import Settings, get_settings
from ..database import SessionLocal
from ..models import Job, JobStatus, SubJob, utcnow
from .db_reporter import DbReporter


class QueueManager:
    """Singleton-style manager owning the executor / RQ connection."""

    def __init__(self) -> None:
        self._settings: Settings = get_settings()
        self._backend = self._settings.resolved_queue_backend()
        self._executor: ThreadPoolExecutor | None = None
        self._design_executor: ThreadPoolExecutor | None = None
        self._rq_queue = None
        self._lock = threading.Lock()
        self._init_backend()

    # ---- backend setup ---------------------------------------------------

    def _init_backend(self) -> None:
        if self._backend == "rq":
            try:
                import redis  # type: ignore
                from rq import Queue  # type: ignore

                conn = redis.Redis.from_url(self._settings.REDIS_URL)
                conn.ping()
                self._rq_queue = Queue("md", connection=conn)
            except Exception:
                # Redis became unreachable between resolution and setup: degrade to local.
                self._backend = "local"

        if self._backend == "local":
            # One worker per MD-pool slot (GPUs in the MD pool × parallel-MD concurrency), plus
            # headroom for design-job orchestrators which run on the design pool.
            pools = self._settings.resolved_gpu_pools()
            md_gpus = sum(1 for p in pools.values() if p == "md") or 1
            design_gpus = sum(1 for p in pools.values() if p == "design")
            md_slots = md_gpus * self._settings.resolved_md_concurrency()
            workers = max(1, md_slots + max(1, design_gpus))
            self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="md-local")

    @property
    def backend(self) -> str:
        return self._backend

    # ---- enqueue ---------------------------------------------------------

    def enqueue(self, subjob_id: str) -> None:
        """Enqueue a subjob for execution per the resolved backend."""
        if self._backend == "rq" and self._rq_queue is not None:
            self._rq_queue.enqueue(
                "mdworker.tasks.run_subjob_task",
                subjob_id,
                job_timeout="24h",
                result_ttl=86400,
            )
            return
        # local
        assert self._executor is not None
        self._executor.submit(self._run_local, subjob_id)

    def enqueue_design(self, design_id: str) -> None:
        """Run a peptide-design job. Always executes in-process (the orchestrator runs the GA
        loop, fans docking out to threads, and drives MD on the design-pool GPU); RQ mode gets a
        lazily-created single-thread executor so design never blocks the MD queue."""
        if self._executor is not None:
            self._executor.submit(self._run_design_local, design_id)
            return
        # Lazily create the single-worker design executor under the lock (double-checked) so
        # concurrent requests can't each build one and break the single-worker serialization.
        if self._design_executor is None:
            with self._lock:
                if self._design_executor is None:
                    self._design_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="design")
        self._design_executor.submit(self._run_design_local, design_id)

    def _run_design_local(self, design_id: str) -> None:
        from .design_service import run_design_job
        run_design_job(design_id)

    # ---- local execution -------------------------------------------------

    def _run_local(self, subjob_id: str) -> None:
        """Run a subjob in-process via the worker runner + DbReporter.

        Import of the worker runner is deferred to here so backend startup never
        depends on the worker being installed. Any import/runtime failure marks the
        subjob (and, if all siblings failed, the job) as failed with a clear message.
        """
        reporter = DbReporter(SessionLocal)
        try:
            from mdworker.pipeline.runner import run_subjob  # type: ignore
        except Exception as exc:  # noqa: BLE001
            self._fail_subjob(
                subjob_id,
                reporter,
                f"Worker runner unavailable: {exc.__class__.__name__}: {exc}. "
                "Install the worker with 'pip install -e ./worker'.",
            )
            return

        # Decide how to call run_subjob ONCE from its signature (no post-failure
        # retry, which could otherwise re-execute a partially-run subjob).
        settings_payload = self._runner_settings()
        kwargs: dict[str, Any] = {"reporter": reporter}
        try:
            sig = inspect.signature(run_subjob)
            params = sig.parameters
            accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            if "settings" in params or accepts_var_kw:
                kwargs["settings"] = settings_payload
        except (TypeError, ValueError):
            # Non-introspectable callable: pass settings; the runner contract (§5)
            # specifies it accepts settings.
            kwargs["settings"] = settings_payload

        try:
            run_subjob(subjob_id, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._fail_subjob(subjob_id, reporter, f"Subjob execution error: {exc}")

    def _runner_settings(self) -> dict[str, Any]:
        s = self._settings
        return {
            "STORAGE_ROOT": s.STORAGE_ROOT,
            "MD_ENGINE": s.resolved_md_engine(),
            "MD_MOCK_SPEEDUP": s.MD_MOCK_SPEEDUP,
            "TRAJECTORY_OUTPUT_PS": s.TRAJECTORY_OUTPUT_PS,
            "PROTEIN_FORCE_FIELD": s.PROTEIN_FORCE_FIELD,
            "LIGAND_FORCE_FIELD": s.LIGAND_FORCE_FIELD,
            "LIGAND_CHARGE_METHOD": s.LIGAND_CHARGE_METHOD,
            "WATER_MODEL": s.WATER_MODEL,
            "PROTEIN_FORCE_FIELD_FALLBACK": s.PROTEIN_FORCE_FIELD_FALLBACK,
            "WATER_MODEL_FALLBACK": s.WATER_MODEL_FALLBACK,
            "FORCEFIELD_AUTOFALLBACK": s.FORCEFIELD_AUTOFALLBACK,
            "BOX_PADDING_NM": s.BOX_PADDING_NM,
            "NVT_STEPS": s.NVT_STEPS,
            "NPT_STEPS": s.NPT_STEPS,
            "MDP_TEMPLATE_DIR": s.MDP_TEMPLATE_DIR,
            "BACKEND_URL": s.BACKEND_URL,
            "INTERNAL_API_TOKEN": s.INTERNAL_API_TOKEN,
        }

    def _fail_subjob(self, subjob_id: str, reporter: DbReporter, message: str) -> None:
        """Best-effort: release any GPU and mark the subjob failed."""
        try:
            reporter.release_gpu(subjob_id)
        except Exception:
            pass
        reporter.log(self._job_id_of(subjob_id) or subjob_id, subjob_id, "error", "queue", message)
        reporter.set_subjob_status(
            subjob_id,
            status=JobStatus.FAILED,
            error_message=message,
            completed_at=utcnow(),
        )
        self._maybe_finalize_job(subjob_id)

    def _job_id_of(self, subjob_id: str) -> str | None:
        db = SessionLocal()
        try:
            sub = db.get(SubJob, subjob_id)
            return sub.job_id if sub else None
        finally:
            db.close()

    def _maybe_finalize_job(self, subjob_id: str) -> None:
        """If every subjob of the parent job is terminal, set the job's terminal status."""
        db = SessionLocal()
        try:
            sub = db.get(SubJob, subjob_id)
            if sub is None:
                return
            siblings = db.query(SubJob).filter(SubJob.job_id == sub.job_id).all()
            if not siblings:
                return
            if any(s.status not in JobStatus.TERMINAL_SET for s in siblings):
                return
            job = db.get(Job, sub.job_id)
            if job is None:
                return
            if all(s.status == JobStatus.COMPLETED for s in siblings):
                job.status = JobStatus.COMPLETED
            elif any(s.status == JobStatus.COMPLETED for s in siblings):
                job.status = JobStatus.COMPLETED  # partial success still yields downloadable results
            else:
                job.status = JobStatus.FAILED
            job.completed_at = utcnow()
            if job.status == JobStatus.COMPLETED and job.result_path is None:
                from .storage import job_dir
                job.result_path = str(job_dir(sub.job_id))
            db.commit()
        finally:
            db.close()

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=False)
        if self._design_executor is not None:
            self._design_executor.shutdown(wait=False, cancel_futures=False)


# Process-global manager, lazily created.
_manager: QueueManager | None = None
_manager_lock = threading.Lock()


def get_queue_manager() -> QueueManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = QueueManager()
    return _manager


def reset_queue_manager() -> None:
    """Testing helper: drop the cached manager so settings changes take effect."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.shutdown()
        _manager = None
