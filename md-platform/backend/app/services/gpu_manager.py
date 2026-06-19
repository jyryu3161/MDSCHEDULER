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

from sqlalchemy import case, func, inspect, select, text, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import GpuPool, GpuStatus, GpuStatusEnum, JobStatus, SubJob, utcnow

# GPU scheduling states that block new assignments regardless of free slots.
_BLOCKED_STATES = (GpuStatusEnum.DISABLED, GpuStatusEnum.MAINTENANCE, GpuStatusEnum.ERROR)


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


def ensure_gpu_columns(db: Session) -> None:
    """Add the pool/capacity/running_count columns to a pre-existing gpustatus table.

    create_all() only creates missing *tables*, not missing *columns*, so a DB created before
    GPU pools existed needs these added. SQLite + PostgreSQL both support ADD COLUMN with a
    default; this is a no-op once the columns are present.
    """
    bind = db.get_bind()
    existing = {c["name"] for c in inspect(bind).get_columns("gpustatus")}
    ddl = {
        "pool": f"ALTER TABLE gpustatus ADD COLUMN pool VARCHAR(16) NOT NULL DEFAULT '{GpuPool.MD}'",
        "capacity": "ALTER TABLE gpustatus ADD COLUMN capacity INTEGER NOT NULL DEFAULT 1",
        "running_count": "ALTER TABLE gpustatus ADD COLUMN running_count INTEGER NOT NULL DEFAULT 0",
    }
    for col, stmt in ddl.items():
        if col not in existing:
            db.execute(text(stmt))
    db.commit()


def seed_gpus(db: Session) -> None:
    """Create gpustatus rows and apply the configured pool/capacity mapping (idempotent)."""
    settings = get_settings()
    ensure_gpu_columns(db)
    samples = query_nvidia_smi()
    pools = settings.resolved_gpu_pools()
    md_capacity = settings.resolved_md_concurrency()

    def _capacity(pool: str) -> int:
        # MD pool honors the configured parallel-MD concurrency; design runs one MD at a time
        # per device (its orchestrator parallelizes across the design GPUs it holds).
        return md_capacity if pool == GpuPool.MD else 1

    def _status(pool: str) -> str:
        return GpuStatusEnum.DISABLED if pool == GpuPool.EXCLUDED else GpuStatusEnum.AVAILABLE

    if samples:
        rows = [(s.gpu_id, s.name, s.utilization, s.memory_used, s.memory_total, s.temperature)
                for s in samples]
    else:
        n = settings.resolved_num_gpus()
        rows = [(gid, f"GPU-{gid} (placeholder)", 0.0, 0.0, 0.0, 0.0) for gid in range(n)]

    for gid, name, util, mem_u, mem_t, temp in rows:
        pool = pools.get(gid, GpuPool.MD)
        row = db.get(GpuStatus, gid)
        if row is None:
            db.add(GpuStatus(
                gpu_id=gid, name=name, status=_status(pool), utilization=util,
                memory_used=mem_u, memory_total=mem_t, temperature=temp,
                pool=pool, capacity=_capacity(pool), running_count=0, updated_at=utcnow(),
            ))
        else:
            # Re-apply pool from config, but PRESERVE an MD GPU's existing capacity so a
            # dashboard concurrency change survives restart/reseed. The env default only
            # (re)initializes capacity when a GPU is newly in the MD pool; design/excluded
            # GPUs are always capacity 1.
            old_pool = row.pool
            if old_pool != pool and row.running_count > 0:
                # Reassigning a pool while the GPU still holds slots: reconcile's min(n,capacity)
                # clamp can hide an active subjob if the new pool's capacity is smaller. Warn so
                # the operator knows to drain the GPU before changing DESIGN_GPU_IDS/MD_GPU_IDS.
                import logging
                logging.getLogger("mdplatform.backend").warning(
                    "GPU %d moved %s->%s while running_count=%d; drain it before reassigning pools "
                    "to avoid slot-count drift.", gid, old_pool, pool, row.running_count)
            row.pool = pool
            # Preserve a GPU's runtime capacity (a dashboard change) across restart/reseed as long
            # as it stays in the same pool — applies to BOTH the MD and the design pool so a
            # per-GPU/per-pool concurrency change survives. Capacity is only (re)initialized to the
            # pool default when a GPU freshly joins a pool; excluded GPUs are always capacity 1.
            if pool == GpuPool.EXCLUDED:
                row.capacity = 1
            elif old_pool != pool:
                row.capacity = _capacity(pool)     # freshly joined this pool -> env/default
            # else: same pool as before -> keep row.capacity (runtime dashboard value)
            if pool == GpuPool.EXCLUDED and row.running_count == 0:
                row.status = GpuStatusEnum.DISABLED
            elif row.status == GpuStatusEnum.DISABLED and pool != GpuPool.EXCLUDED and row.running_count == 0:
                row.status = GpuStatusEnum.AVAILABLE
    db.commit()
    reconcile_running_counts(db)


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


def reconcile_running_counts(db: Session) -> None:
    """Recompute every GPU's ``running_count`` from the subjobs actually assigned to it.

    A crashed worker can leave a stale slot reserved; this resets counts to the truth (active
    subjobs holding that GPU) and re-derives status (busy when full, else available unless
    admin-disabled). Safe to call on startup and after seeding.
    """
    active_counts = dict(
        db.execute(
            select(SubJob.assigned_gpu, func.count())
            .where(SubJob.assigned_gpu.is_not(None),
                   SubJob.status.in_(JobStatus.RUNNING_SET))
            .group_by(SubJob.assigned_gpu)
        ).all()
    )
    for row in db.execute(select(GpuStatus)).scalars():
        n = int(active_counts.get(row.gpu_id, 0))
        row.running_count = min(n, row.capacity) if row.capacity else 0
        if row.status not in _BLOCKED_STATES:
            row.status = GpuStatusEnum.BUSY if row.running_count >= max(1, row.capacity) else GpuStatusEnum.AVAILABLE
        row.updated_at = utcnow()
    db.commit()


def request_gpu(db: Session, subjob_id: str, pool: str = GpuPool.MD) -> int | None:
    """Atomically claim a free slot on a GPU in ``pool`` for ``subjob_id``.

    Slots are counted: a GPU is claimable while ``running_count < capacity`` and it is not
    admin-blocked. The claim is a single conditional UPDATE guarded on ``running_count <
    capacity`` (race-safe on SQLite + PostgreSQL via the affected-row count), which increments
    the count and flips status to 'busy' once the GPU fills. The least-loaded GPU in the pool
    is preferred so parallel MD spreads across devices. Returns the gpu_id or None when the
    pool has no free slot. Idempotent: a subjob that already holds a GPU keeps it.
    """
    sub = db.get(SubJob, subjob_id)
    if sub is not None and sub.assigned_gpu is not None:
        return sub.assigned_gpu

    max_attempts = max(1, int(
        db.execute(select(func.count()).select_from(GpuStatus).where(GpuStatus.pool == pool)).scalar_one() or 0
    ))
    for _ in range(max_attempts):
        candidate_id = db.execute(
            select(GpuStatus.gpu_id)
            .where(GpuStatus.pool == pool,
                   GpuStatus.status.notin_(_BLOCKED_STATES),
                   GpuStatus.running_count < GpuStatus.capacity)
            .order_by(GpuStatus.running_count, GpuStatus.gpu_id)
            .limit(1)
        ).scalar_one_or_none()
        if candidate_id is None:
            return None

        # Atomically take a slot (guarded on running_count < capacity).
        new_count = GpuStatus.running_count + 1
        took = db.execute(
            update(GpuStatus)
            .where(GpuStatus.gpu_id == candidate_id,
                   GpuStatus.pool == pool,
                   GpuStatus.status.notin_(_BLOCKED_STATES),
                   GpuStatus.running_count < GpuStatus.capacity)
            .values(
                running_count=new_count,
                status=case((new_count >= GpuStatus.capacity, GpuStatusEnum.BUSY),
                            else_=GpuStatusEnum.AVAILABLE),
                updated_at=utcnow(),
            )
        )
        if took.rowcount != 1:
            db.commit()  # lost the race for this GPU; pick another
            continue

        # Bind the slot to the subjob with a conditional UPDATE so two concurrent requests for
        # the SAME subjob can't both keep a slot: only the one that flips assigned_gpu NULL->id
        # wins; the loser releases the slot it just took and returns the winner's GPU.
        if sub is not None:
            bound = db.execute(
                update(SubJob)
                .where(SubJob.id == subjob_id, SubJob.assigned_gpu.is_(None))
                .values(assigned_gpu=candidate_id)
            )
            if bound.rowcount != 1:
                _decrement_slot(db, candidate_id)
                db.commit()
                existing = db.execute(
                    select(SubJob.assigned_gpu).where(SubJob.id == subjob_id)
                ).scalar_one_or_none()
                return existing
            db.execute(update(GpuStatus).where(GpuStatus.gpu_id == candidate_id)
                       .values(assigned_subjob_id=subjob_id))
        else:
            # No SubJob row (non-subjob caller): use the legacy marker for idempotency.
            db.execute(update(GpuStatus).where(GpuStatus.gpu_id == candidate_id)
                       .values(assigned_subjob_id=subjob_id))
        db.commit()
        return candidate_id
    return None


def _decrement_slot(db: Session, gpu_id: int) -> None:
    """Atomically free one slot on ``gpu_id`` (running_count-1 floored at 0; reopen if not blocked).

    Uses a portable CASE (no SQLite MAX()/PostgreSQL GREATEST split) so the decrement is a
    single race-safe statement.
    """
    dec = case((GpuStatus.running_count > 0, GpuStatus.running_count - 1), else_=0)
    db.execute(
        update(GpuStatus).where(GpuStatus.gpu_id == gpu_id).values(
            running_count=dec,
            status=case((GpuStatus.status.in_(_BLOCKED_STATES), GpuStatus.status),
                        else_=GpuStatusEnum.AVAILABLE),
            updated_at=utcnow(),
        )
    )


def release_gpu(db: Session, subjob_id: str, *, commit: bool = True) -> bool:
    """Free the slot held by ``subjob_id`` on its GPU. Returns True if a slot was freed.

    The subjob's ``assigned_gpu`` (set atomically at claim time) is the source of truth. The
    binding is cleared with a conditional UPDATE (``assigned_gpu == gid``) and the GPU slot is
    decremented ONLY when that clear affects exactly one row, so concurrent/duplicate releases
    cannot double-decrement. Falls back to the legacy ``assigned_subjob_id`` marker for rows
    claimed without a SubJob.

    ``commit=False`` performs the release WITHOUT committing, so it can participate in a caller's
    larger transaction (e.g. cancel/retry releasing several subjobs atomically); the caller then
    commits once.
    """
    def _flush() -> None:
        if commit:
            db.commit()

    sub = db.get(SubJob, subjob_id)
    gid = sub.assigned_gpu if sub is not None else None
    if gid is not None:
        cleared = db.execute(
            update(SubJob)
            .where(SubJob.id == subjob_id, SubJob.assigned_gpu == gid)
            .values(assigned_gpu=None)
        )
        if cleared.rowcount != 1:
            _flush()  # someone already released it
            return False
        _decrement_slot(db, gid)
        db.execute(update(GpuStatus).where(GpuStatus.gpu_id == gid,
                                           GpuStatus.assigned_subjob_id == subjob_id)
                   .values(assigned_subjob_id=None))
        _flush()
        return True

    # Legacy single-occupancy fallback: claim the clear via the marker, then decrement.
    legacy_gid = db.execute(
        select(GpuStatus.gpu_id).where(GpuStatus.assigned_subjob_id == subjob_id)
    ).scalar_one_or_none()
    if legacy_gid is None:
        return False
    cleared = db.execute(
        update(GpuStatus)
        .where(GpuStatus.gpu_id == legacy_gid, GpuStatus.assigned_subjob_id == subjob_id)
        .values(assigned_subjob_id=None)
    )
    if cleared.rowcount != 1:
        _flush()
        return False
    _decrement_slot(db, legacy_gid)
    _flush()
    return True


def set_pool_capacity(db: Session, pool: str, capacity: int) -> list[GpuStatus]:
    """Set the per-GPU slot capacity for every GPU in ``pool`` (runtime parallel-MD control).

    Lowering capacity never evicts running subjobs; a GPU already above the new capacity simply
    stops accepting new claims until it drains. Returns the affected rows.
    """
    cap = max(1, int(capacity))
    rows = list(db.execute(select(GpuStatus).where(GpuStatus.pool == pool)).scalars())
    for row in rows:
        row.capacity = cap
        if row.status not in _BLOCKED_STATES:
            row.status = GpuStatusEnum.BUSY if row.running_count >= cap else GpuStatusEnum.AVAILABLE
        row.updated_at = utcnow()
    db.commit()
    return rows


def set_gpu_capacity(db: Session, gpu_id: int, capacity: int) -> GpuStatus | None:
    """Set ONE GPU's slot capacity (per-GPU parallel-run control; any pool).

    Mirrors ``set_pool_capacity`` but scoped to a single device so an operator can tune individual
    GPUs from the dashboard (e.g. give a stronger card more concurrent MD/design slots). Lowering
    capacity never evicts running subjobs — the GPU simply stops accepting new claims until it
    drains below the new limit. Returns the updated row, or None if the GPU doesn't exist.
    """
    cap = max(1, min(16, int(capacity)))
    row = db.get(GpuStatus, gpu_id)
    if row is None:
        return None
    row.capacity = cap
    if row.status not in _BLOCKED_STATES:
        row.status = GpuStatusEnum.BUSY if row.running_count >= cap else GpuStatusEnum.AVAILABLE
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    return row


class GpuBusyError(Exception):
    """Raised when a pool reassignment is attempted on a GPU that still holds running slots."""


def set_gpu_pool(db: Session, gpu_id: int, pool: str) -> GpuStatus | None:
    """Reassign a GPU to a pool ("md" | "design" | "excluded") at runtime (dashboard control).

    Only allowed when the GPU is IDLE (running_count == 0): reassigning a GPU that still holds
    slots would corrupt the per-pool slot accounting (see the reconcile clamp), so the caller
    must drain it first. Sets the pool, a sane capacity for the new pool (MD keeps the current
    MD concurrency default; design/excluded = 1), and status (excluded -> disabled). Raises
    GpuBusyError if busy; returns None if the GPU doesn't exist.
    """
    if pool not in GpuPool.ALL:
        raise ValueError(f"Unknown pool {pool!r}; expected one of {GpuPool.ALL}.")
    capacity = get_settings().resolved_md_concurrency() if pool == GpuPool.MD else 1
    new_status = GpuStatusEnum.DISABLED if pool == GpuPool.EXCLUDED else GpuStatusEnum.AVAILABLE
    # Single conditional UPDATE guarded on running_count == 0, so a scheduler claiming a slot
    # between a read and the write can't slip a running job onto a GPU we're reassigning
    # (race-safe, mirrors request_gpu's atomic claim).
    result = db.execute(
        update(GpuStatus)
        .where(GpuStatus.gpu_id == gpu_id, GpuStatus.running_count == 0)
        .values(pool=pool, capacity=capacity, status=new_status, updated_at=utcnow())
    )
    db.commit()
    if result.rowcount == 1:
        return db.get(GpuStatus, gpu_id)  # expire_on_commit reloads fresh values
    # 0 rows affected: the GPU is missing, or it still holds a running slot.
    row = db.get(GpuStatus, gpu_id)
    if row is None:
        return None
    raise GpuBusyError(f"GPU {gpu_id} has {row.running_count} running slot(s); drain it before reassigning its pool.")


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
