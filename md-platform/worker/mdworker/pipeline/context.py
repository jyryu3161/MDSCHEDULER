"""Reporter seam + JobContext (CONTRACT §5 "Worker↔Backend seam", §8 storage layout, §9).

The worker NEVER imports backend internals. It talks to the backend through the Reporter
Protocol. The default implementation is HttpReporter, which POSTs to the Internal API
(`/api/internal/*`) using BACKEND_URL + X-Internal-Token. The backend ships a DbReporter
implementing the identical method signatures for the in-process LocalExecutor.

JobContext wraps a Reporter plus the on-disk paths for one subjob (one pose), exposing
log/status/progress helpers that the pipeline steps call.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

import httpx


# --------------------------------------------------------------------------------------
# Reporter Protocol — EXACT signatures from CONTRACT.md §5.
# --------------------------------------------------------------------------------------
@runtime_checkable
class Reporter(Protocol):
    def set_subjob_status(
        self,
        subjob_id,
        *,
        status=None,
        current_step=None,
        progress=None,
        completed_ns=None,
        ns_per_day=None,
        assigned_gpu=None,
        error_message=None,
        started_at=None,
        completed_at=None,
        result_path=None,
    ) -> None: ...

    def set_job_status(
        self,
        job_id,
        *,
        status=None,
        result_path=None,
        error_message=None,
        started_at=None,
        completed_at=None,
    ) -> None: ...

    def log(self, job_id, subjob_id, level, step, message) -> None: ...

    def request_gpu(self, subjob_id) -> "int | None": ...  # None when no GPU free

    def release_gpu(self, subjob_id) -> None: ...

    def is_cancelled(self, subjob_id) -> bool: ...  # True once the backend cancelled the subjob


class JobCancelled(Exception):
    """Raised inside the pipeline when the subjob has been cancelled by the user/admin.

    The runner catches it, finalizes the subjob as ``cancelled`` (not ``failed``), and frees
    the GPU. Engines raise it after killing any in-flight subprocess (real GROMACS run) or
    breaking their progress loop (mock), so a cancel actually stops the computation rather than
    only flipping the database row.
    """


# --------------------------------------------------------------------------------------
# HttpReporter — default; posts to the Internal API.
# --------------------------------------------------------------------------------------
class HttpReporter:
    """Reporter that POSTs to the backend Internal API (CONTRACT §5 internal endpoints).

    All requests carry the `X-Internal-Token` header. Network errors are swallowed after a
    single retry so that a transient backend hiccup does not crash a long MD run; the
    failure is printed to stderr for operator visibility. Status/log loss is non-fatal to
    the simulation itself, and the final completed/failed status is retried harder.
    """

    def __init__(
        self,
        backend_url: str,
        internal_api_token: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self.backend_url = backend_url.rstrip("/")
        self._token = internal_api_token
        self._timeout = timeout
        self._max_retries = max_retries
        self._client = httpx.Client(
            timeout=timeout,
            headers={"X-Internal-Token": internal_api_token},
        )

    # -- low-level ---------------------------------------------------------------------
    def _post(self, path: str, payload: dict, *, critical: bool = False) -> Optional[dict]:
        url = f"{self.backend_url}{path}"
        attempts = self._max_retries + (3 if critical else 0)
        last_exc: Optional[Exception] = None
        for _ in range(max(1, attempts)):
            try:
                resp = self._client.post(url, json=payload)
                resp.raise_for_status()
                if resp.content:
                    try:
                        return resp.json()
                    except json.JSONDecodeError:
                        return None
                return None
            except Exception as exc:  # noqa: BLE001 - reporting must not crash the run
                last_exc = exc
        # Exhausted retries: log to stderr (never raise from a reporter call).
        print(
            f"[mdworker.HttpReporter] POST {path} failed after retries: {last_exc}",
            flush=True,
        )
        return None

    @staticmethod
    def _compact(payload: dict) -> dict:
        return {k: v for k, v in payload.items() if v is not None}

    # -- Reporter interface ------------------------------------------------------------
    def set_subjob_status(
        self,
        subjob_id,
        *,
        status=None,
        current_step=None,
        progress=None,
        completed_ns=None,
        ns_per_day=None,
        assigned_gpu=None,
        error_message=None,
        started_at=None,
        completed_at=None,
        result_path=None,
    ) -> None:
        payload = self._compact(
            {
                "status": status,
                "current_step": current_step,
                "progress": progress,
                "completed_ns": completed_ns,
                "ns_per_day": ns_per_day,
                "assigned_gpu": assigned_gpu,
                "error_message": error_message,
                "started_at": started_at,
                "completed_at": completed_at,
                "result_path": result_path,
            }
        )
        critical = status in ("completed", "failed", "cancelled")
        self._post(f"/api/internal/subjobs/{subjob_id}/status", payload, critical=critical)

    def set_job_status(
        self,
        job_id,
        *,
        status=None,
        result_path=None,
        error_message=None,
        started_at=None,
        completed_at=None,
    ) -> None:
        payload = self._compact(
            {
                "status": status,
                "result_path": result_path,
                "error_message": error_message,
                "started_at": started_at,
                "completed_at": completed_at,
            }
        )
        critical = status in ("completed", "failed", "cancelled")
        self._post(f"/api/internal/jobs/{job_id}/status", payload, critical=critical)

    def log(self, job_id, subjob_id, level, step, message) -> None:
        payload = {
            "job_id": job_id,
            "subjob_id": subjob_id,
            "level": level,
            "step": step,
            "message": message,
        }
        self._post("/api/internal/logs", payload)

    def request_gpu(self, subjob_id) -> "int | None":
        result = self._post(
            "/api/internal/gpus/request", {"subjob_id": subjob_id}, critical=True
        )
        if not result:
            return None
        return result.get("gpu_id")

    def release_gpu(self, subjob_id) -> None:
        self._post("/api/internal/gpus/release", {"subjob_id": subjob_id}, critical=True)

    def is_cancelled(self, subjob_id) -> bool:
        """Poll the backend for a cancel signal. Returns False on any transient error so a
        network hiccup never falsely kills a healthy run (cancellation is re-checked)."""
        try:
            resp = self._client.get(
                f"{self.backend_url}/api/internal/subjobs/{subjob_id}/cancelled"
            )
            resp.raise_for_status()
            return bool(resp.json().get("cancelled"))
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------------------
# JobContext — paths (CONTRACT §8) + reporter delegation helpers.
# --------------------------------------------------------------------------------------
class JobContext:
    """Per-subjob (per-pose) execution context.

    Owns the storage paths for one pose under
    ``{STORAGE_ROOT}/jobs/{job_id}/pose_{NN}/`` (CONTRACT §8) and delegates
    status/log/progress to the Reporter. Steps receive a JobContext and only touch their
    own pose dir + the shared job dir.
    """

    def __init__(
        self,
        *,
        job_id: str,
        subjob_id: str,
        pose_index: int,
        storage_root: str,
        reporter: Reporter,
        job_meta: dict,
        subjob_meta: dict,
    ) -> None:
        self.job_id = job_id
        self.subjob_id = subjob_id
        self.pose_index = int(pose_index)
        self.reporter = reporter
        self.job_meta = job_meta or {}
        self.subjob_meta = subjob_meta or {}

        self.storage_root = Path(storage_root)
        self.job_dir = self.storage_root / "jobs" / job_id
        # pose dir is zero-padded 2-digit, 1-based (CONTRACT §8: pose_01, pose_02, ...)
        self.pose_name = f"pose_{self.pose_index:02d}"
        self.pose_dir = self.job_dir / self.pose_name

        # Standard subdirectories per CONTRACT §8.
        self.prep_dir = self.pose_dir / "prep"
        self.md_dir = self.pose_dir / "md"
        self.analysis_dir = self.pose_dir / "analysis"
        self.plots_dir = self.analysis_dir / "plots"
        self.viz_dir = self.pose_dir / "visualization"
        self.logs_dir = self.pose_dir / "logs"

        self.input_dir = self.job_dir / "input"
        self.input_original_dir = self.input_dir / "original"
        self.input_processed_dir = self.input_dir / "processed"
        self.summary_dir = self.job_dir / "summary"

    # -- filesystem --------------------------------------------------------------------
    def ensure_dirs(self) -> None:
        for d in (
            self.pose_dir,
            self.prep_dir,
            self.md_dir,
            self.analysis_dir,
            self.plots_dir,
            self.viz_dir,
            self.logs_dir,
            self.summary_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # -- reporter delegation -----------------------------------------------------------
    def log(self, level: str, step: str, message: str) -> None:
        """Emit a log line to the backend and append to the pose logfile."""
        self.reporter.log(self.job_id, self.subjob_id, level, step, message)
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            with (self.logs_dir / "pipeline.log").open("a", encoding="utf-8") as fh:
                fh.write(f"[{level}] [{step}] {message}\n")
        except Exception:  # noqa: BLE001 - never let logging crash the pipeline
            pass

    def info(self, step: str, message: str) -> None:
        self.log("info", step, message)

    def warning(self, step: str, message: str) -> None:
        self.log("warning", step, message)

    def error(self, step: str, message: str) -> None:
        self.log("error", step, message)

    def set_status(
        self,
        status: Optional[str] = None,
        *,
        current_step: Optional[str] = None,
        progress: Optional[float] = None,
        completed_ns: Optional[float] = None,
        ns_per_day: Optional[float] = None,
        assigned_gpu: Optional[int] = None,
        error_message: Optional[str] = None,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        result_path: Optional[str] = None,
    ) -> None:
        self.reporter.set_subjob_status(
            self.subjob_id,
            status=status,
            current_step=current_step,
            progress=progress,
            completed_ns=completed_ns,
            ns_per_day=ns_per_day,
            assigned_gpu=assigned_gpu,
            error_message=error_message,
            started_at=started_at,
            completed_at=completed_at,
            result_path=result_path,
        )

    def progress(
        self,
        value: float,
        *,
        current_step: Optional[str] = None,
        completed_ns: Optional[float] = None,
        ns_per_day: Optional[float] = None,
    ) -> None:
        self.reporter.set_subjob_status(
            self.subjob_id,
            progress=round(float(value), 2),
            current_step=current_step,
            completed_ns=completed_ns,
            ns_per_day=ns_per_day,
        )

    def request_gpu(self) -> "int | None":
        return self.reporter.request_gpu(self.subjob_id)

    def release_gpu(self) -> None:
        self.reporter.release_gpu(self.subjob_id)

    def is_cancelled(self, *, min_interval_s: float = 1.0) -> bool:
        """Whether the backend has cancelled this subjob (cached for ``min_interval_s`` to
        avoid hammering the API while polling a long-running subprocess)."""
        import time as _time

        now = _time.monotonic()
        last = getattr(self, "_cancel_checked_at", 0.0)
        if getattr(self, "_cancel_cached", False):
            return True  # cancellation is sticky
        if now - last < min_interval_s:
            return False
        self._cancel_checked_at = now
        try:
            cancelled = bool(self.reporter.is_cancelled(self.subjob_id))
        except Exception:  # noqa: BLE001
            cancelled = False
        if cancelled:
            self._cancel_cached = True
        return cancelled

    def check_cancelled(self) -> None:
        """Raise JobCancelled if the subjob has been cancelled (call at safe points)."""
        if self.is_cancelled():
            raise JobCancelled(f"Subjob {self.subjob_id} was cancelled.")

    # -- metadata helpers --------------------------------------------------------------
    @property
    def md_length_ns(self) -> float:
        return float(self.job_meta.get("md_length_ns", 50))

    @property
    def ligand_type(self) -> str:
        return str(self.job_meta.get("ligand_type", "small_molecule"))

    @property
    def docking_score(self):
        return self.subjob_meta.get("docking_score")

    def job_path(self, *parts: str) -> Path:
        return self.job_dir.joinpath(*parts)

    def pose_path(self, *parts: str) -> Path:
        return self.pose_dir.joinpath(*parts)


def find_input_file(ctx: JobContext, *candidates: str) -> Optional[Path]:
    """Locate one of the candidate filenames in input/original then input/processed."""
    for base in (ctx.input_original_dir, ctx.input_processed_dir, ctx.input_dir):
        for name in candidates:
            p = base / name
            if p.exists():
                return p
    return None
