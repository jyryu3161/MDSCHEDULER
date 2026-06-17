"""Results router (CONTRACT §5 Results)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import Job, PlotType, Role, SubJob, User
from ..schemas import JobOut, JobResults, SubJobResult, SubJobResultDetail
from ..services import storage

router = APIRouter(prefix="/jobs", tags=["results"])


def _get_owned_job(db: Session, job_id: str, user: User) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    if job.user_id != user.id and user.role != Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return job


def _analysis_summary(job_id: str, pose_index: int, replica_index: int = 1) -> dict:
    """Return the per-pose FLAT scalar metrics for the UI summary cards + comparison table.

    The worker's analysis/summary.json nests scalar metrics under ``metrics`` alongside
    structural objects (``data_source``, ``plots_available``); the API exposes only the flat
    scalar metrics so the frontend renders them directly without hitting non-scalar values.
    """
    pdir = storage.pose_dir(job_id, pose_index, replica_index)
    summary = storage.read_json(pdir / "analysis" / "summary.json")
    if not isinstance(summary, dict):
        return {}
    metrics = summary.get("metrics")
    source = metrics if isinstance(metrics, dict) else summary
    return {k: v for k, v in source.items() if not isinstance(v, (dict, list))}


def _plots_available(job_id: str, pose_index: int, replica_index: int = 1) -> list[str]:
    plots_dir = storage.pose_dir(job_id, pose_index, replica_index) / "analysis" / "plots"
    available: list[str] = []
    if plots_dir.exists():
        present = {p.stem for p in plots_dir.glob("*.json")}
        for pt in PlotType.ALL:
            if pt in present:
                available.append(pt)
    return available


def _mmpbsa(job_id: str, pose_index: int, replica_index: int = 1) -> dict | None:
    """Per-pose MM/PBSA/MM/GBSA binding ΔG (analysis/mmpbsa.json), or None if not computed."""
    data = storage.read_json(storage.pose_dir(job_id, pose_index, replica_index) / "analysis" / "mmpbsa.json")
    return data or None


def _per_residue(job_id: str, pose_index: int, replica_index: int = 1) -> dict | None:
    """Per-residue ΔG decomposition (analysis/per_residue.json) — binding hotspots."""
    data = storage.read_json(storage.pose_dir(job_id, pose_index, replica_index) / "analysis" / "per_residue.json")
    return data or None


def _bound_window(job_id: str, pose_index: int, replica_index: int = 1) -> dict | None:
    """Auto-detected bound window from analysis/summary.json (leading in-pocket segment)."""
    summary = storage.read_json(storage.pose_dir(job_id, pose_index, replica_index) / "analysis" / "summary.json")
    if isinstance(summary, dict):
        bw = summary.get("bound_window")
        if isinstance(bw, dict):
            return bw
    return None


def _residue_key(chain, resnum) -> tuple:
    """Normalize a (chain, resnum) merge key across the ΔG and contact sources."""
    try:
        rn = int(resnum)
    except (TypeError, ValueError):
        rn = resnum
    return (str(chain or "").strip(), rn)


def _hotspots(job_id: str, pose_index: int, replica_index: int = 1) -> list[dict]:
    """Unified hotspot table: merge per-residue ΔG (MM/PBSA) with geometric contact
    frequency + mean H-bonds (analysis/residue_contacts.json, over the bound window) by
    residue. Either source may be absent; we union both and sort by ΔG (most favorable
    first), falling back to contact frequency when ΔG is unavailable."""
    pdir = storage.pose_dir(job_id, pose_index, replica_index)
    per_res = storage.read_json(pdir / "analysis" / "per_residue.json")
    contacts = storage.read_json(pdir / "analysis" / "residue_contacts.json")

    merged: dict[tuple, dict] = {}
    if isinstance(per_res, dict):
        for r in per_res.get("residues", []):
            if not isinstance(r, dict):
                continue
            key = _residue_key(r.get("chain"), r.get("resnum"))
            merged[key] = {
                "chain": r.get("chain"), "resname": r.get("resname"), "resnum": r.get("resnum"),
                "total_dg": r.get("total_dg"), "vdw": r.get("vdw"), "eel": r.get("eel"),
                "contact_frequency": None, "hbond_mean": None,
            }
    if isinstance(contacts, dict):
        for r in contacts.get("residues", []):
            if not isinstance(r, dict):
                continue
            key = _residue_key(r.get("chain"), r.get("resnum"))
            row = merged.setdefault(key, {
                "chain": r.get("chain"), "resname": r.get("resname"), "resnum": r.get("resnum"),
                "total_dg": None, "vdw": None, "eel": None,
            })
            row["contact_frequency"] = r.get("contact_frequency")
            row["hbond_mean"] = r.get("hbond_mean")
            row.setdefault("resname", r.get("resname"))

    rows = []
    for row in merged.values():
        rn = row.get("resname") or "RES"
        rs = row.get("resnum")
        row["residue"] = f"{rn}{rs}"
        rows.append(row)

    def _sort_key(r: dict):
        dg = r.get("total_dg")
        cf = r.get("contact_frequency") or 0.0
        # ΔG present → rank by ΔG ascending (most negative/favorable first); group those above
        # residues that only have contact data, which rank by contact frequency descending.
        return (0, dg) if isinstance(dg, (int, float)) else (1, -cf)

    rows.sort(key=_sort_key)
    return rows


def _trajectory_path(job_id: str, pose_index: int, replica_index: int = 1) -> Path | None:
    vis = storage.pose_dir(job_id, pose_index, replica_index) / "visualization"
    pdb = vis / "trajectory.pdb"
    if pdb.exists():
        return pdb
    return None


def _movie_path(job_id: str, pose_index: int, replica_index: int = 1) -> Path | None:
    vis = storage.pose_dir(job_id, pose_index, replica_index) / "visualization"
    for cand in ("movie.mp4", "movie.webm", "trajectory.mp4", "trajectory.webm"):
        p = vis / cand
        if p.exists():
            return p
    return None


def _subjob_result(job_id: str, sj: SubJob) -> SubJobResult:
    return SubJobResult(
        id=sj.id,
        job_id=sj.job_id,
        pose_index=sj.pose_index,
        docking_score=sj.docking_score,
        status=sj.status,
        progress=sj.progress,
        completed_ns=sj.completed_ns,
        ns_per_day=sj.ns_per_day,
        result_path=sj.result_path,
        error_message=sj.error_message,
        analysis_summary=_analysis_summary(job_id, sj.pose_index, sj.replica_index),
        plots_available=_plots_available(job_id, sj.pose_index, sj.replica_index),
        has_trajectory=_trajectory_path(job_id, sj.pose_index, sj.replica_index) is not None,
        has_movie=_movie_path(job_id, sj.pose_index, sj.replica_index) is not None,
        mmpbsa=_mmpbsa(job_id, sj.pose_index, sj.replica_index),
        per_residue=_per_residue(job_id, sj.pose_index, sj.replica_index),
        bound_window=_bound_window(job_id, sj.pose_index, sj.replica_index),
        hotspots=_hotspots(job_id, sj.pose_index, sj.replica_index),
    )


@router.get("/{job_id}/results", response_model=JobResults)
def job_results(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JobResults:
    job = _get_owned_job(db, job_id, user)
    subjobs = (
        db.execute(select(SubJob).where(SubJob.job_id == job_id).order_by(SubJob.pose_index))
        .scalars()
        .all()
    )
    return JobResults(
        job=JobOut.model_validate(job),
        subjobs=[_subjob_result(job_id, sj) for sj in subjobs],
    )


@router.get("/{job_id}/subjobs/{subjob_id}/results", response_model=SubJobResultDetail)
def subjob_results(
    job_id: str,
    subjob_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SubJobResultDetail:
    _get_owned_job(db, job_id, user)
    sj = db.get(SubJob, subjob_id)
    if sj is None or sj.job_id != job_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SubJob not found.")

    # Pull this pose's row from the job-level pose comparison if present.
    comparison_entry: dict = {}
    comp = storage.read_json(storage.summary_dir(job_id) / "pose_comparison.json")
    if isinstance(comp, dict):
        for row in comp.get("rows", []):
            if str(row.get("pose_index")) == str(sj.pose_index) or row.get("subjob_id") == subjob_id:
                comparison_entry = row
                break

    return SubJobResultDetail(
        subjob=_subjob_result(job_id, sj),
        pose_comparison=comparison_entry,
    )


@router.get("/{job_id}/download")
def download_all(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    _get_owned_job(db, job_id, user)
    # Prefer a pre-built all_results.zip; otherwise stream the whole job tree.
    prebuilt = storage.summary_dir(job_id) / "all_results.zip"
    if prebuilt.exists():
        return FileResponse(
            path=str(prebuilt),
            media_type="application/zip",
            filename=f"{job_id}_all_results.zip",
        )
    stream = storage.stream_zip_of_directory(storage.job_dir(job_id), job_id)
    return StreamingResponse(
        stream,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{job_id}_all_results.zip"'},
    )


@router.get("/{job_id}/subjobs/{subjob_id}/download")
def download_pose(
    job_id: str,
    subjob_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    _get_owned_job(db, job_id, user)
    sj = db.get(SubJob, subjob_id)
    if sj is None or sj.job_id != job_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SubJob not found.")
    pdir = storage.pose_dir(job_id, sj.pose_index, sj.replica_index)
    prebuilt = pdir / "results.zip"
    if prebuilt.exists():
        return FileResponse(
            path=str(prebuilt),
            media_type="application/zip",
            filename=f"{subjob_id}_results.zip",
        )
    stream = storage.stream_zip_of_directory(pdir, storage.pose_dirname(sj.pose_index, sj.replica_index))
    return StreamingResponse(
        stream,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{subjob_id}_results.zip"'},
    )


@router.get("/{job_id}/plots/{plot_type}")
def get_plot(
    job_id: str,
    plot_type: str,
    subjob_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    _get_owned_job(db, job_id, user)
    if plot_type not in PlotType.ALL:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown plot type '{plot_type}'.")

    if subjob_id:
        sj = db.get(SubJob, subjob_id)
        if sj is None or sj.job_id != job_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SubJob not found.")
        fig = storage.read_json(
            storage.pose_dir(job_id, sj.pose_index, sj.replica_index) / "analysis" / "plots" / f"{plot_type}.json"
        )
        if not fig:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plot not available.")
        return fig

    # Pose-comparison overlay: prefer a job-level overlay file, else synthesize an
    # overlay by merging each pose's traces into one Plotly figure.
    overlay = storage.read_json(storage.summary_dir(job_id) / "plots" / f"{plot_type}.json")
    if overlay:
        return overlay

    # One trace per pose (the canonical replica 1) so the cross-pose overlay isn't cluttered by
    # every replica of every pose.
    subjobs = db.execute(
        select(SubJob).where(SubJob.job_id == job_id, SubJob.replica_index == 1)
        .order_by(SubJob.pose_index)
    ).scalars().all()
    merged_data: list = []
    layout: dict = {}
    for sj in subjobs:
        fig = storage.read_json(
            storage.pose_dir(job_id, sj.pose_index, sj.replica_index) / "analysis" / "plots" / f"{plot_type}.json"
        )
        if not fig:
            continue
        if not layout and isinstance(fig.get("layout"), dict):
            layout = dict(fig["layout"])
        for trace in fig.get("data", []):
            trace = dict(trace)
            base_name = trace.get("name") or plot_type
            trace["name"] = f"pose {sj.pose_index}: {base_name}"
            merged_data.append(trace)
    if not merged_data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plot not available.")
    layout.setdefault("title", f"{plot_type.upper()} — pose comparison")
    return {"data": merged_data, "layout": layout}


@router.get("/{job_id}/trajectory")
def get_trajectory(
    job_id: str,
    subjob_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    _get_owned_job(db, job_id, user)
    pose_index, replica_index = _resolve_pose(db, job_id, subjob_id)
    pdb = _trajectory_path(job_id, pose_index, replica_index)
    if pdb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trajectory not available.")
    return FileResponse(
        path=str(pdb),
        media_type="chemical/x-pdb",
        filename="trajectory.pdb",
        headers={"X-Trajectory-Format": "pdb"},
    )


@router.get("/{job_id}/movie")
def get_movie(
    job_id: str,
    subjob_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    _get_owned_job(db, job_id, user)
    pose_index, replica_index = _resolve_pose(db, job_id, subjob_id)
    movie = _movie_path(job_id, pose_index, replica_index)
    if movie is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Movie not available.")
    media_type = "video/mp4" if movie.suffix == ".mp4" else "video/webm"
    return FileResponse(path=str(movie), media_type=media_type, filename=movie.name)


def _resolve_pose(db: Session, job_id: str, subjob_id: str | None) -> tuple[int, int]:
    """Resolve (pose_index, replica_index) from subjob_id, or default to the best (first) pose's
    canonical replica 1. Replica-aware so trajectory/movie serve the requested replica, not just
    replica 1."""
    if subjob_id:
        sj = db.get(SubJob, subjob_id)
        if sj is None or sj.job_id != job_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SubJob not found.")
        return sj.pose_index, sj.replica_index
    first = (
        db.execute(
            select(SubJob).where(SubJob.job_id == job_id)
            .order_by(SubJob.pose_index, SubJob.replica_index).limit(1)
        )
        .scalars()
        .first()
    )
    if first is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No poses for this job.")
    return first.pose_index, first.replica_index
