"""Uploads router (CONTRACT §5 Uploads)."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from ..config import Settings, get_settings
from ..deps import get_current_user
from ..models import Role, User
from ..schemas import UploadResponse, ValidationReport
from ..services import storage, validation

router = APIRouter(prefix="/uploads", tags=["uploads"])

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
_CHUNK = 1024 * 1024


def _new_upload_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"upload_{ts}_{secrets.token_hex(4)}"


def _safe_filename(name: str | None, fallback: str) -> str:
    if not name:
        return fallback
    base = Path(name).name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or fallback
    return cleaned


async def _save_upload(dest_dir: Path, field_name: str, upload: UploadFile, max_bytes: int) -> str:
    """Stream an UploadFile to disk with a size guard. Returns stored filename."""
    filename = _safe_filename(upload.filename, field_name)
    dest = dest_dir / filename
    written = 0
    with dest.open("wb") as fh:
        while True:
            chunk = await upload.read(_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                fh.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Upload exceeds the maximum allowed size of {max_bytes // (1024**3)} GB.",
                )
            fh.write(chunk)
    await upload.close()
    return filename


@router.post("/input", response_model=UploadResponse)
async def upload_input(
    pose_file: UploadFile = File(..., description="AutoDock Vina PDBQT with poses (required)"),
    chemistry_file: UploadFile | None = File(default=None, description="SDF/MOL2 chemistry template"),
    receptor_file: UploadFile | None = File(default=None, description="Receptor PDB/CIF"),
    smiles: str | None = Form(default=None),
    name: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> UploadResponse:
    upload_id = _new_upload_id()
    dest_dir = storage.upload_dir(upload_id)
    storage.ensure_dirs(dest_dir)

    max_bytes = settings.max_upload_bytes
    pose_name = await _save_upload(dest_dir, "pose.pdbqt", pose_file, max_bytes)

    chem_name = None
    if chemistry_file is not None and chemistry_file.filename:
        chem_name = await _save_upload(dest_dir, "chemistry", chemistry_file, max_bytes)

    rec_name = None
    if receptor_file is not None and receptor_file.filename:
        rec_name = await _save_upload(dest_dir, "receptor", receptor_file, max_bytes)

    # Persist a manifest so /validate and job creation can reconstruct paths.
    meta = {
        "upload_id": upload_id,
        "user_id": user.id,
        "name": name,
        "pose_file": pose_name,
        "chemistry_file": chem_name,
        "receptor_file": rec_name,
        "smiles": smiles,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    storage.write_json(dest_dir / "meta.json", meta)

    # Run validation to populate the detection fields the UI needs immediately.
    report = validation.run_validation(
        pose_file=dest_dir / pose_name,
        chemistry_file=(dest_dir / chem_name) if chem_name else None,
        receptor_file=(dest_dir / rec_name) if rec_name else None,
        smiles=smiles,
        settings=settings,
    )

    return UploadResponse(
        upload_id=upload_id,
        pose_file=pose_name,
        chemistry_file=chem_name,
        receptor_file=rec_name,
        detected_pose_count=report.pose_count,
        detected_input_type=report.input_type,
        ligand_type_candidates=report.ligand_type_candidates,
        hetatm_candidates=report.hetatm_candidates,
    )


@router.get("/{upload_id}/validate", response_model=ValidationReport)
def validate_upload(
    upload_id: str,
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> ValidationReport:
    dest_dir = storage.upload_dir(upload_id)
    meta = storage.read_json(dest_dir / "meta.json")
    if not meta:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found.")

    # Authorization: only the owner (or an admin) may validate an upload.
    if meta.get("user_id") != user.id and user.role != Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found.")

    pose_name = meta.get("pose_file")
    chem_name = meta.get("chemistry_file")
    rec_name = meta.get("receptor_file")
    smiles = meta.get("smiles")

    report = validation.run_validation(
        pose_file=(dest_dir / pose_name) if pose_name else None,
        chemistry_file=(dest_dir / chem_name) if chem_name else None,
        receptor_file=(dest_dir / rec_name) if rec_name else None,
        smiles=smiles,
        settings=settings,
    )
    return report
