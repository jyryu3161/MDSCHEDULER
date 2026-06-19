"""DesignReporter — backend-side persistence seam for the peptide-design runner.

Mirrors DbReporter: the worker's ``mdworker.design.runner`` calls these methods (structural
Protocol, no import coupling) and each opens a short-lived session via the factory. Brokers a
GPU from the DESIGN pool, persists generation candidates (upsert by sequence), and exposes the
cancel flag the runner polls between generations.
"""

from __future__ import annotations

from typing import Callable, List

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import realtime
from ..models import DesignCandidate, DesignJob, GpuPool, JobStatus, utcnow
from . import gpu_manager


class DesignReporter:
    def __init__(self, session_factory: Callable[[], Session]):
        self._session_factory = session_factory

    def _publish(self, design_id: str) -> None:
        try:
            realtime.publish_from_thread(realtime.job_topic(design_id),
                                         {"type": "design", "design_id": design_id})
            realtime.publish_from_thread(realtime.dashboard_topic(),
                                         {"trigger": "design_update", "design_id": design_id})
        except Exception:  # noqa: BLE001 — realtime is best-effort
            pass

    def set_status(self, design_id: str, status: str, *, error_message: str | None = None,
                   current_generation: int | None = None) -> None:
        db = self._session_factory()
        try:
            dj = db.get(DesignJob, design_id)
            if dj is None:
                return
            # Always-safe fields (telemetry), applied regardless of terminal state.
            if error_message is not None:
                dj.error_message = error_message
            if current_generation is not None:
                dj.current_generation = current_generation
            # Resurrection guard via compare-and-swap: never overwrite a terminal design job — e.g.
            # one the user cancelled — with a late status from the runner finishing its current
            # generation. The status (and its terminal/running side-effects) apply ONLY when the
            # CAS actually transitions a non-terminal row.
            res = db.execute(
                update(DesignJob)
                .where(DesignJob.id == design_id, DesignJob.status.notin_(JobStatus.TERMINAL_SET))
                .values(status=status)
            )
            if (res.rowcount or 0) > 0:
                if status in JobStatus.RUNNING_SET and dj.started_at is None:
                    dj.started_at = utcnow()
                if status in JobStatus.TERMINAL_SET:
                    dj.completed_at = utcnow()
                    if status == JobStatus.COMPLETED:
                        dj.progress = 100.0
            db.commit()
        finally:
            db.close()
        self._publish(design_id)

    def set_progress(self, design_id: str, progress: float, *, current_generation: int | None = None) -> None:
        db = self._session_factory()
        try:
            dj = db.get(DesignJob, design_id)
            if dj is None:
                return
            dj.progress = float(progress)
            if current_generation is not None:
                dj.current_generation = current_generation
            db.commit()
        finally:
            db.close()
        self._publish(design_id)

    def record_candidates(self, design_id: str, rows: List[dict]) -> None:
        """Upsert candidates keyed by (design_job_id, sequence, generation): insert new, update
        ΔG/fitness/refined.

        Keyed by (sequence, generation) — NOT sequence alone — so a sequence re-evaluated in a
        later generation gets its OWN row instead of overwriting the earlier generation's. This
        preserves per-generation history, which the convergence curve (best-so-far per generation)
        depends on; keying by sequence alone collapsed all generations of a recurring sequence
        into one row and corrupted that curve."""
        db = self._session_factory()
        try:
            existing = {
                (c.sequence, c.generation): c for c in db.execute(
                    select(DesignCandidate).where(DesignCandidate.design_job_id == design_id)
                ).scalars()
            }
            for r in rows:
                seq = r["sequence"]
                gen = int(r.get("generation", 0))
                c = existing.get((seq, gen))
                if c is None:
                    c = DesignCandidate(design_job_id=design_id, sequence=seq, generation=gen)
                    db.add(c)
                    existing[(seq, gen)] = c
                c.docking_score = r.get("docking_score")
                c.md_dg = r.get("md_dg")
                c.fitness = float(r.get("fitness", 0.0) or 0.0)
                c.refined = bool(r.get("refined", False))
            db.commit()
        finally:
            db.close()
        self._publish(design_id)

    def set_result(self, design_id: str, *, best_sequence: str | None, best_fitness: float,
                   best_docking_score, best_md_dg, result_path: str) -> None:
        db = self._session_factory()
        try:
            dj = db.get(DesignJob, design_id)
            if dj is None:
                return
            dj.best_sequence = best_sequence
            dj.best_fitness = best_fitness
            dj.best_docking_score = best_docking_score
            dj.best_md_dg = best_md_dg
            dj.result_path = result_path
            db.commit()
        finally:
            db.close()
        self._publish(design_id)

    def request_gpu(self, design_id: str) -> int | None:
        db = self._session_factory()
        try:
            gid = gpu_manager.request_gpu(db, design_id, pool=GpuPool.DESIGN)
            if gid is not None:
                dj = db.get(DesignJob, design_id)
                if dj is not None:
                    dj.assigned_gpu = gid
                    db.commit()
            return gid
        finally:
            db.close()

    def release_gpu(self, design_id: str) -> None:
        db = self._session_factory()
        try:
            gpu_manager.release_gpu(db, design_id)
            dj = db.get(DesignJob, design_id)
            if dj is not None:
                dj.assigned_gpu = None
                db.commit()
        finally:
            db.close()

    def is_cancelled(self, design_id: str) -> bool:
        db = self._session_factory()
        try:
            dj = db.get(DesignJob, design_id)
            return dj is not None and dj.status == JobStatus.CANCELLED
        finally:
            db.close()

    def log(self, design_id: str, level: str, step: str, message: str) -> None:
        from ..models import JobLog
        db = self._session_factory()
        try:
            db.add(JobLog(job_id=design_id, subjob_id=None, level=level, step=step, message=message))
            db.commit()
        finally:
            db.close()
