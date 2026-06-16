"""GPU router (CONTRACT §5 GPU)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import GpuStatusEnum, User
from ..schemas import GpuStatusOut
from ..services import gpu_manager

router = APIRouter(prefix="/gpus", tags=["gpus"])


@router.get("", response_model=list[GpuStatusOut])
def list_gpus(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[GpuStatusOut]:
    return [GpuStatusOut.model_validate(g) for g in gpu_manager.list_gpus(db)]


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
