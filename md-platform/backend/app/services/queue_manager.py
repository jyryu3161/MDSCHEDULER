"""Queue manager — enqueue subjobs to RQ or an in-process ThreadPoolExecutor.

CONTRACT §5 enqueue contract:
  - rq mode    : rq.Queue.enqueue('mdworker.tasks.run_subjob_task', subjob_id, settings)
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
        # Current worker-thread counts + executors retired by a runtime grow (kept referenced so
        # they drain their in-flight/queued work instead of being GC'd mid-task).
        self._md_workers = 0
        self._design_workers = 0
        self._retired_executors: list[ThreadPoolExecutor] = []
        self._rq_queue = None
        self._design_queue = None
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
                self._design_queue = Queue("design", connection=conn)
            except Exception:
                # Redis became unreachable between resolution and setup: degrade to local.
                self._backend = "local"

        if self._backend == "local":
            # The MD executor gets EXACTLY one worker per MD-pool GPU slot (sum of the MD GPUs'
            # capacities). It must NOT exceed the slot count: an extra worker would start a subjob
            # that can't get a GPU lock — a real-GROMACS job then queues in _acquire_gpu (and would
            # eventually fail), so excess work belongs in the executor's own queue, not as a spare
            # worker. Sizing from the live DB capacities (not the static env default) means a
            # dashboard concurrency change governs real parallelism; sync_capacity() grows the pool
            # when capacity is raised at runtime. Design orchestrators run on a SEPARATE executor so
            # they never consume MD worker slots (which would starve MD and inflate its concurrency).
            md_workers, design_workers = self._desired_workers()
            self._md_workers = md_workers
            self._design_workers = design_workers
            self._executor = ThreadPoolExecutor(max_workers=md_workers, thread_name_prefix="md-local")
            # Dedicated design pool: one worker per design GPU slot (>=1). enqueue_design() targets this.
            self._design_executor = ThreadPoolExecutor(
                max_workers=design_workers, thread_name_prefix="design")

    def _desired_workers(self) -> tuple[int, int]:
        """(md_workers, design_workers) sized to the *current* total GPU-pool slot capacity.

        One worker per GPU slot, so request_gpu stays the authoritative concurrency gate and excess
        subjobs wait in the executor's own queue. Reads the live per-GPU capacities from the DB so a
        dashboard change drives real parallelism; falls back to the env-configured pool layout before
        the GPU table is seeded (or if the query fails). Both counts are floored at 1 so the executors
        always have a worker (a design job submitted with no design GPU still runs CPU/mock)."""
        md = design = 0
        try:
            from sqlalchemy import func, select

            from ..database import SessionLocal
            from ..models import GpuPool, GpuStatus
            db = SessionLocal()
            try:
                rows = db.execute(
                    select(GpuStatus.pool, func.coalesce(func.sum(GpuStatus.capacity), 0))
                    .group_by(GpuStatus.pool)
                ).all()
                by_pool = {p: int(c) for p, c in rows}
                md = by_pool.get(GpuPool.MD, 0)
                design = by_pool.get(GpuPool.DESIGN, 0)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 — DB not ready yet; use the env fallback below
            md = design = 0
        if md <= 0 or design <= 0:
            pools = self._settings.resolved_gpu_pools()
            if md <= 0:
                md_gpus = sum(1 for p in pools.values() if p == "md") or 1
                md = md_gpus * self._settings.resolved_md_concurrency()
            if design <= 0:
                design = sum(1 for p in pools.values() if p == "design")
        return max(1, md), max(1, design)

    def sync_capacity(self) -> None:
        """Resize the local executors to match current DB GPU-pool capacities after a dashboard
        capacity change. ThreadPoolExecutor can't shrink live, so this only GROWS: a capacity
        *increase* gets more worker threads immediately (new submissions use the larger pool; the
        retired smaller pool drains its in-flight/queued work). A capacity *decrease* takes effect
        immediately for scheduling via request_gpu's slot gate; the surplus idle worker threads are
        reclaimed on the next restart. No-op in RQ mode."""
        if self._backend != "local":
            return
        with self._lock:
            md_workers, design_workers = self._desired_workers()
            if self._executor is not None and md_workers > self._md_workers:
                self._executor.shutdown(wait=False, cancel_futures=False)
                self._retired_executors.append(self._executor)
                self._executor = ThreadPoolExecutor(max_workers=md_workers, thread_name_prefix="md-local")
                self._md_workers = md_workers
            if self._design_executor is not None and design_workers > self._design_workers:
                self._design_executor.shutdown(wait=False, cancel_futures=False)
                self._retired_executors.append(self._design_executor)
                self._design_executor = ThreadPoolExecutor(
                    max_workers=design_workers, thread_name_prefix="design")
                self._design_workers = design_workers

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
                self._runner_settings(),
                job_timeout="24h",
                result_ttl=86400,
            )
            return
        # local — submit under the lock so a concurrent sync_capacity() resize can't swap/shutdown
        # the executor between our read and our submit (which would raise and drop the subjob).
        with self._lock:
            assert self._executor is not None
            self._executor.submit(self._run_local, subjob_id)

    def enqueue_design(self, design_id: str) -> None:
        """Enqueue a peptide-design job.

        RQ mode sends the task to the worker image (scientific toolchain + GPU runtime). Local mode
        keeps using the backend's dedicated DESIGN executor, so design orchestrators never consume MD
        worker slots.
        """
        if self._backend == "rq" and self._design_queue is not None:
            from .design_service import design_task_payload

            config, settings_payload = design_task_payload(design_id)
            self._design_queue.enqueue(
                "mdworker.tasks.run_design_task",
                design_id,
                config,
                settings_payload,
                job_timeout="72h",
                result_ttl=86400,
            )
            return
        # Lazily create the design executor under the lock (double-checked) if it doesn't exist
        # yet, then submit under the same lock so a concurrent sync_capacity() resize can't
        # swap/shutdown it between our read and our submit (which would raise and drop the design
        # job).
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

    def _gemini_config(self) -> tuple[str, str]:
        """Admin-configured Gemini key/model (DB override) with env fallback; never raises."""
        try:
            from ..database import SessionLocal
            from . import settings_store
            db = SessionLocal()
            try:
                return settings_store.gemini_config(db)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 — report config must not block enqueue
            return (self._settings.GEMINI_API_KEY or "", self._settings.GEMINI_MODEL or "gemini-3.5-flash")

    def _runner_settings(self) -> dict[str, Any]:
        s = self._settings
        gemini_key, gemini_model = self._gemini_config()
        return {
            "GEMINI_API_KEY": gemini_key,
            "GEMINI_MODEL": gemini_model,
            "REPORT_ENABLED": s.REPORT_ENABLED,
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
        for ex in (self._executor, self._design_executor, *self._retired_executors):
            if ex is not None:
                ex.shutdown(wait=False, cancel_futures=False)


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
