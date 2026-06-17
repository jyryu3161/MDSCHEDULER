"""Peptide-design router (GA): create / list / detail / cancel.

A design job evolves peptides to bind a fixed target compound. The compound is supplied as a
structure-file upload (.sdf/.mol/.mol2/.pdb) OR a SMILES string; GA parameters come as form
fields. Runs on the GPU design pool via the queue manager.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..deps import get_current_user
from ..models import DesignCandidate, DesignJob, JobStatus, Role, User, utcnow
from ..schemas import (
    DesignCandidateOut,
    DesignGenerationPoint,
    DesignJobCreate,
    DesignJobDetail,
    DesignJobOut,
)
from ..services import storage
from ..services.queue_manager import get_queue_manager

router = APIRouter(prefix="/design", tags=["design"])

_id_lock = threading.Lock()
_ALLOWED_COMPOUND_SUFFIXES = {".sdf", ".mol", ".mol2", ".pdb", ".smi"}


def _next_design_id(db: Session) -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    prefix = f"design_{today}_"
    count = db.execute(select(func.count()).select_from(DesignJob).where(DesignJob.id.like(f"{prefix}%"))).scalar_one()
    seq = int(count) + 1
    while db.get(DesignJob, f"{prefix}{seq:03d}") is not None:
        seq += 1
    return f"{prefix}{seq:03d}"


def _owned(db: Session, design_id: str, user: User) -> DesignJob:
    dj = db.get(DesignJob, design_id)
    if dj is None or (dj.user_id != user.id and user.role != Role.ADMIN):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Design job not found.")
    return dj


@router.get("/{design_id}/report")
def get_design_report(
    design_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    """Serve the auto-generated publication report (report.html) for a design run, inline."""
    _owned(db, design_id, user)
    report = storage.storage_root() / "design" / design_id / "report.html"
    if not report.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not available for this design.")
    return FileResponse(path=str(report), media_type="text/html")


@router.post("", response_model=DesignJobOut, status_code=status.HTTP_201_CREATED)
async def create_design(
    name: str = Form(...),
    initial_sequences: str = Form(..., description="comma/space/newline-separated peptides"),
    population_size: int = Form(10),
    num_generations: int = Form(5),
    dock_oversample: int = Form(4),
    md_length_ns: int = Form(10),
    n_replicas: int = Form(1),
    exhaustiveness: int = Form(8),
    eval_mode: str = Form("hybrid"),
    dock_engine: str = Form("vina"),
    strategy: str = Form("ga"),
    compound_name: str = Form("compound"),
    smiles: str | None = Form(None),
    compound: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DesignJobOut:
    seqs = [s for s in initial_sequences.replace(",", " ").split() if s]
    try:
        cfg = DesignJobCreate(
            name=name, initial_sequences=seqs, population_size=population_size,
            num_generations=num_generations, dock_oversample=dock_oversample, md_length_ns=md_length_ns,
            n_replicas=n_replicas, exhaustiveness=exhaustiveness, eval_mode=eval_mode,
            dock_engine=dock_engine, strategy=strategy, smiles=smiles, compound_name=compound_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    if compound is None and not (smiles and smiles.strip()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Provide a compound file or a SMILES string.")

    # Read + size-limit the upload BEFORE taking the lock (never await while holding it).
    compound_suffix: str | None = None
    compound_bytes: bytes | None = None
    if compound is not None:
        compound_suffix = Path(compound.filename or "").suffix.lower()
        if compound_suffix not in _ALLOWED_COMPOUND_SUFFIXES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Unsupported compound type {compound_suffix!r}; allowed: {sorted(_ALLOWED_COMPOUND_SUFFIXES)}.")
        max_bytes = min(get_settings().max_upload_bytes, 25 * 1024 * 1024)  # compounds are tiny
        buf = bytearray()
        while True:
            chunk = await compound.read(1 << 20)
            if not chunk:
                break
            buf += chunk
            if len(buf) > max_bytes:
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                    detail=f"Compound file exceeds {max_bytes} bytes.")
        compound_bytes = bytes(buf)

    # Reserve the id + persist the row under a lock so concurrent creates can't collide.
    # Pure sync work below — no awaits inside the lock.
    with _id_lock:
        design_id = _next_design_id(db)
        ddir = storage.storage_root() / "design" / design_id
        ddir.mkdir(parents=True, exist_ok=True)

        if compound_bytes is not None:
            compound_path = ddir / f"compound{compound_suffix}"
            compound_path.write_bytes(compound_bytes)
        else:
            compound_path = ddir / "compound.smi"
            compound_path.write_text(smiles.strip())

        dj = DesignJob(
            id=design_id, user_id=user.id, name=cfg.name, status=JobStatus.QUEUED,
            compound_name=cfg.compound_name, compound_file=str(compound_path),
            initial_sequences=json.dumps(cfg.initial_sequences),
            peptide_length=len(cfg.initial_sequences[0]),
            population_size=cfg.population_size, num_generations=cfg.num_generations,
            dock_oversample=cfg.dock_oversample, md_length_ns=cfg.md_length_ns, n_replicas=cfg.n_replicas,
            exhaustiveness=cfg.exhaustiveness, eval_mode=cfg.eval_mode, dock_engine=cfg.dock_engine,
            strategy=cfg.strategy,
            created_at=utcnow(),
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

    get_queue_manager().enqueue_design(design_id)
    return DesignJobOut.model_validate(dj)


@router.get("", response_model=list[DesignJobOut])
def list_designs(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[DesignJobOut]:
    q = select(DesignJob).order_by(DesignJob.created_at.desc())
    if user.role != Role.ADMIN:
        q = q.where(DesignJob.user_id == user.id)
    return [DesignJobOut.model_validate(d) for d in db.execute(q).scalars()]


@router.get("/{design_id}", response_model=DesignJobDetail)
def design_detail(
    design_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DesignJobDetail:
    dj = _owned(db, design_id, user)
    candidates = db.execute(
        select(DesignCandidate).where(DesignCandidate.design_job_id == design_id)
    ).scalars().all()
    # Leaderboard: best (lowest fitness rank) first — refined ΔG candidates float to the top.
    leaderboard = sorted(candidates, key=lambda c: -c.fitness)

    # Convergence curve: best-so-far fitness per generation (monotonic).
    by_gen: dict[int, list[DesignCandidate]] = {}
    for c in candidates:
        by_gen.setdefault(c.generation, []).append(c)
    curve: list[DesignGenerationPoint] = []
    running_best: DesignCandidate | None = None
    for gen in sorted(by_gen):
        gen_best = max(by_gen[gen], key=lambda c: c.fitness)
        if running_best is None or gen_best.fitness > running_best.fitness:
            running_best = gen_best
        curve.append(DesignGenerationPoint(
            generation=gen, best_fitness=running_best.fitness, best_sequence=running_best.sequence,
            best_docking_score=running_best.docking_score, best_md_dg=running_best.md_dg,
        ))

    return DesignJobDetail(
        job=DesignJobOut.model_validate(dj),
        candidates=[DesignCandidateOut.model_validate(c) for c in leaderboard],
        generations=curve,
    )


@router.post("/{design_id}/cancel", response_model=DesignJobOut)
def cancel_design(
    design_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DesignJobOut:
    dj = _owned(db, design_id, user)
    if dj.status in JobStatus.TERMINAL_SET:
        return DesignJobOut.model_validate(dj)
    # The runner polls this flag between generations and aborts cooperatively.
    dj.status = JobStatus.CANCELLED
    dj.completed_at = utcnow()
    db.commit()
    db.refresh(dj)
    return DesignJobOut.model_validate(dj)
