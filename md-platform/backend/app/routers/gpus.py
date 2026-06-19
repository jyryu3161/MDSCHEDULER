"""GPU router (CONTRACT §5 GPU)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from pydantic import BaseModel, Field

from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import GpuPool, GpuStatusEnum, User
from ..schemas import GpuStatusOut
from ..services import gpu_manager
from ..services.queue_manager import get_queue_manager

router = APIRouter(prefix="/gpus", tags=["gpus"])


class ConcurrencyUpdate(BaseModel):
    pool: str = GpuPool.MD
    concurrency: int = Field(ge=1, le=16)


class CapacityUpdate(BaseModel):
    capacity: int = Field(ge=1, le=16)


class PoolUpdate(BaseModel):
    pool: Literal["md", "design", "excluded"]


@router.get("", response_model=list[GpuStatusOut])
def list_gpus(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[GpuStatusOut]:
    return [GpuStatusOut.model_validate(g) for g in gpu_manager.list_gpus(db)]


@router.patch("/concurrency", response_model=list[GpuStatusOut])
def set_concurrency(
    body: ConcurrencyUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> list[GpuStatusOut]:
    """Set how many subjobs may run concurrently on each GPU in a pool (parallel-MD control).

    Adjustable from the dashboard; takes effect immediately for new claims (running subjobs are
    never evicted). Returns the updated pool GPUs.
    """
    if body.pool not in (GpuPool.MD, GpuPool.DESIGN):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"pool must be one of: {GpuPool.MD}, {GpuPool.DESIGN}.")
    rows = gpu_manager.set_pool_capacity(db, body.pool, body.concurrency)
    # Grow the in-process executor so the new concurrency takes effect immediately (not just on
    # restart). request_gpu already gates new claims at the new capacity.
    get_queue_manager().sync_capacity()
    return [GpuStatusOut.model_validate(r) for r in rows]


@router.patch("/{gpu_id}/capacity", response_model=GpuStatusOut)
def set_gpu_capacity(
    gpu_id: int,
    body: CapacityUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> GpuStatusOut:
    """Set how many subjobs may run concurrently on a SINGLE GPU (per-GPU parallel-run control).

    Works for any pool (MD or design), so an operator can tune individual devices from the
    dashboard. Takes effect immediately for new claims (running subjobs are never evicted); the
    in-process executor is grown to match so the change governs real parallelism.
    """
    row = gpu_manager.set_gpu_capacity(db, gpu_id, body.capacity)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GPU not found.")
    get_queue_manager().sync_capacity()
    return GpuStatusOut.model_validate(row)


@router.patch("/{gpu_id}/pool", response_model=GpuStatusOut)
def set_gpu_pool(
    gpu_id: int,
    body: PoolUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> GpuStatusOut:
    """Reassign a GPU to the MD / design / excluded pool from the dashboard.

    Only allowed when the GPU is idle (no running slots) — reassigning a busy GPU would corrupt
    per-pool slot accounting; drain it first (409 otherwise).
    """
    try:
        row = gpu_manager.set_gpu_pool(db, gpu_id, body.pool)
    except gpu_manager.GpuBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GPU not found.")
    return GpuStatusOut.model_validate(row)


def _set_status(db: Session, gpu_id: int, target: str) -> GpuStatusOut:
    row = gpu_manager.admin_set_status(db, gpu_id, target)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GPU not found.")
    return GpuStatusOut.model_validate(row)


@router.post("/{gpu_id}/enable", response_model=GpuStatusOut)
def enable_gpu(
    gpu_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> GpuStatusOut:
    return _set_status(db, gpu_id, GpuStatusEnum.AVAILABLE)


@router.post("/{gpu_id}/disable", response_model=GpuStatusOut)
def disable_gpu(
    gpu_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> GpuStatusOut:
    return _set_status(db, gpu_id, GpuStatusEnum.DISABLED)


@router.post("/{gpu_id}/maintenance", response_model=GpuStatusOut)
def maintenance_gpu(
    gpu_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> GpuStatusOut:
    return _set_status(db, gpu_id, GpuStatusEnum.MAINTENANCE)
