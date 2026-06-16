"""GPU detection, status polling, and atomic request/release (CONTRACT §5 GPU/Internal).

nvidia-smi query (exact columns required by the contract):
    nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu
               --format=csv,noheader,nounits

When nvidia-smi is absent, NUM_GPUS placeholder rows are created so the platform
still schedules work (the mock engine ignores real devices).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import GpuStatus, GpuStatusEnum, utcnow


@dataclass
class GpuSample:
    gpu_id: int
    name: str
    utilization: float
    memory_used: float
    memory_total: float
    temperature: float


def query_nvidia_smi() -> list[GpuSample] | None:
    """Run nvidia-smi and parse the required columns. None if unavailable."""
    cmd = [
        "nvidia-smi",
        "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None

    samples: list[GpuSample] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            samples.append(
                GpuSample(
                    gpu_id=int(parts[0]),
                    name=parts[1],
                    utilization=_to_float(parts[2]),
                    memory_used=_to_float(parts[3]),
                    memory_total=_to_float(parts[4]),
                    temperature=_to_float(parts[5]),
                )
            )
        except ValueError:
            continue
    return samples


def _to_float(value: str) -> float:
    value = value.strip()
    if not value or value.upper().startswith("N/A") or value == "[N/A]":
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def seed_gpus(db: Session) -> None:
    """Create gpustatus rows from nvidia-smi or NUM_GPUS placeholders (idempotent)."""
    settings = get_settings()
    samples = query_nvidia_smi()

    if samples:
        for s in samples:
            row = db.get(GpuStatus, s.gpu_id)
            if row is None:
                row = GpuStatus(
                    gpu_id=s.gpu_id,
                    name=s.name,
                    status=GpuStatusEnum.AVAILABLE,
                    utilization=s.utilization,
                    memory_used=s.memory_used,
                    memory_total=s.memory_total,
                    temperature=s.temperature,
                    updated_at=utcnow(),
                )
                db.add(row)
        db.commit()
        return

    # Placeholder rows.
    n = settings.resolved_num_gpus()
    for gpu_id in range(n):
        row = db.get(GpuStatus, gpu_id)
        if row is None:
            db.add(
                GpuStatus(
                    gpu_id=gpu_id,
                    name=f"GPU-{gpu_id} (placeholder)",
                    status=GpuStatusEnum.AVAILABLE,
                    utilization=0.0,
                    memory_used=0.0,
                    memory_total=0.0,
                    temperature=0.0,
                    updated_at=utcnow(),
                )
            )
    db.commit()


def poll_and_update(db: Session) -> None:
    """Refresh live metrics for existing rows from nvidia-smi.

    Only updates utilization/memory/temperature; never flips scheduling state
    (available/busy/disabled/maintenance), which is owned by request/release and
    admin actions.
    """
    samples = query_nvidia_smi()
    if not samples:
        return
    for s in samples:
        row = db.get(GpuStatus, s.gpu_id)
        if row is None:
            continue
        row.name = s.name or row.name
        row.utilization = s.utilization
        row.memory_used = s.memory_used
        row.memory_total = s.memory_total
        row.temperature = s.temperature
        row.updated_at = utcnow()
    db.commit()


def list_gpus(db: Session) -> list[GpuStatus]:
    return list(db.execute(select(GpuStatus).order_by(GpuStatus.gpu_id)).scalars())


def request_gpu(db: Session, subjob_id: str) -> int | None:
    """Atomically claim an available GPU for ``subjob_id``.

    Picks the lowest-id GPU whose status is 'available' and flips it to
    'busy'+assigned using a *conditional* UPDATE guarded on the still-available
    state. The UPDATE's affected-row count is the source of truth: if another
    caller claimed the same GPU first, rowcount is 0 and we retry the selection.
    This is race-safe on both SQLite and PostgreSQL (no dialect-specific FOR
    UPDATE / RETURNING needed). Returns gpu_id or None when none free.

    Idempotent: if the subjob already holds a GPU, that gpu_id is returned.
    """
    # If this subjob already owns a GPU, return it (handles retries/double-calls).
    existing = db.execute(
        select(GpuStatus).where(GpuStatus.assigned_subjob_id == subjob_id)
    ).scalars().first()
    if existing is not None:
        return existing.gpu_id

    # Retry bound: at most the real GPU count. Each failed claim means another
    # caller took that specific GPU, so after N attempts every GPU has been
    # eliminated or claimed; this terminates without ever giving up while a GPU
    # remains genuinely available.
    gpu_count = db.execute(select(func.count()).select_from(GpuStatus)).scalar_one() or 0
    max_attempts = max(1, int(gpu_count))
    for _ in range(max_attempts):
        candidate_id = db.execute(
            select(GpuStatus.gpu_id)
            .where(GpuStatus.status == GpuStatusEnum.AVAILABLE)
            .order_by(GpuStatus.gpu_id)
            .limit(1)
        ).scalar_one_or_none()
        if candidate_id is None:
            return None

        result = db.execute(
            update(GpuStatus)
            .where(
                GpuStatus.gpu_id == candidate_id,
                GpuStatus.status == GpuStatusEnum.AVAILABLE,
            )
            .values(
                status=GpuStatusEnum.BUSY,
                assigned_subjob_id=subjob_id,
                updated_at=utcnow(),
            )
        )
        db.commit()
        if result.rowcount == 1:
            return candidate_id
        # rowcount 0 -> lost the race for this GPU; loop and pick another.
    return None


def release_gpu(db: Session, subjob_id: str) -> bool:
    """Free any GPU held by ``subjob_id`` -> 'available'. Returns True if one freed."""
    rows = db.execute(
        select(GpuStatus).where(GpuStatus.assigned_subjob_id == subjob_id)
    ).scalars().all()
    freed = False
    for row in rows:
        # Only revert scheduling for GPUs we legitimately hold busy.
        if row.status == GpuStatusEnum.BUSY:
            row.status = GpuStatusEnum.AVAILABLE
        row.assigned_subjob_id = None
        row.updated_at = utcnow()
        freed = True
    if freed:
        db.commit()
    return freed


def set_gpu_state(db: Session, gpu_id: int, *, status: str, subjob_id: str | None = None) -> GpuStatus | None:
    """Directly set a GPU's scheduling state (used by /internal/gpus/{id}/assign)."""
    row = db.get(GpuStatus, gpu_id)
    if row is None:
        return None
    row.status = status
    row.assigned_subjob_id = subjob_id
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    return row


def admin_set_status(db: Session, gpu_id: int, target: str) -> GpuStatus | None:
    """Admin enable/disable/maintenance transitions.

    'enable' -> available (only if not currently busy with a real subjob).
    """
    row = db.get(GpuStatus, gpu_id)
    if row is None:
        return None
    if target == GpuStatusEnum.AVAILABLE and row.status == GpuStatusEnum.BUSY:
        # Don't yank a GPU out from under a running subjob.
        return row
    row.status = target
    if target in (GpuStatusEnum.DISABLED, GpuStatusEnum.MAINTENANCE, GpuStatusEnum.AVAILABLE):
        row.assigned_subjob_id = None
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    return row
