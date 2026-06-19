"""HTTP reporter for design workers running outside the backend process."""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx


class HttpDesignReporter:
    """Reporter used by RQ design tasks.

    It mirrors the backend-side DesignReporter API, but persists through the Internal API so design
    jobs run in the worker image instead of the backend container.
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
        self._max_retries = max_retries
        self._client = httpx.Client(
            timeout=timeout,
            headers={"X-Internal-Token": internal_api_token},
        )

    def _post(self, path: str, payload: dict[str, Any], *, critical: bool = False) -> Optional[dict]:
        attempts = self._max_retries + (3 if critical else 0)
        last_exc: Optional[Exception] = None
        for _ in range(max(1, attempts)):
            try:
                resp = self._client.post(f"{self.backend_url}{path}", json=payload)
                resp.raise_for_status()
                if not resp.content:
                    return None
                try:
                    return resp.json()
                except json.JSONDecodeError:
                    return None
            except Exception as exc:  # noqa: BLE001 - progress reporting must not crash compute
                last_exc = exc
        print(f"[mdworker.HttpDesignReporter] POST {path} failed after retries: {last_exc}", flush=True)
        return None

    @staticmethod
    def _compact(payload: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in payload.items() if v is not None}

    def set_status(
        self,
        design_id: str,
        status: str,
        *,
        error_message: str | None = None,
        current_generation: int | None = None,
    ) -> None:
        self._post(
            f"/api/internal/design/{design_id}/status",
            self._compact({
                "status": status,
                "error_message": error_message,
                "current_generation": current_generation,
            }),
            critical=status in ("completed", "failed", "cancelled"),
        )

    def set_progress(
        self,
        design_id: str,
        progress: float,
        *,
        current_generation: int | None = None,
    ) -> None:
        self._post(
            f"/api/internal/design/{design_id}/progress",
            self._compact({"progress": progress, "current_generation": current_generation}),
        )

    def record_candidates(self, design_id: str, rows: list[dict]) -> None:
        self._post(f"/api/internal/design/{design_id}/candidates", {"rows": rows})

    def set_result(
        self,
        design_id: str,
        *,
        best_sequence: str | None,
        best_fitness: float,
        best_docking_score,
        best_md_dg,
        result_path: str,
    ) -> None:
        self._post(
            f"/api/internal/design/{design_id}/result",
            self._compact({
                "best_sequence": best_sequence,
                "best_fitness": best_fitness,
                "best_docking_score": best_docking_score,
                "best_md_dg": best_md_dg,
                "result_path": result_path,
            }),
            critical=True,
        )

    def request_gpu(self, design_id: str) -> int | None:
        result = self._post(
            "/api/internal/gpus/request",
            {"subjob_id": design_id, "pool": "design"},
            critical=True,
        )
        if not result:
            return None
        return result.get("gpu_id")

    def release_gpu(self, design_id: str) -> None:
        self._post("/api/internal/gpus/release", {"subjob_id": design_id}, critical=True)

    def is_cancelled(self, design_id: str) -> bool:
        try:
            resp = self._client.get(f"{self.backend_url}/api/internal/design/{design_id}/cancelled")
            resp.raise_for_status()
            return bool(resp.json().get("cancelled"))
        except Exception:  # noqa: BLE001
            return False

    def log(self, design_id: str, level: str, step: str, message: str) -> None:
        self._post(
            "/api/internal/logs",
            {"job_id": design_id, "subjob_id": None, "level": level, "step": step, "message": message},
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass
