"""Step 8 — render_movie (CONTRACT §9.8).

Guarantees the multi-MODEL ``visualization/trajectory.pdb`` exists for the NGL/Mol* viewer
(the engine writes it there; if a real GROMACS run produced it elsewhere it is copied in).
A rendered movie (mp4/webm) is optional: it is produced only if a renderer is available, and
its absence is NOT a failure — the interactive trajectory viewer is the MVP primary per
PDR §16.3.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict


def run(ctx, settings, *, md: Dict[str, Any]) -> Dict[str, Any]:
    step = "render_movie"
    ctx.set_status("rendering", current_step=step, progress=94.0)

    viz_traj = ctx.viz_dir / "trajectory.pdb"
    src = md.get("trajectory_pdb_path")
    if src and Path(src).resolve() != viz_traj.resolve() and Path(src).exists():
        ctx.viz_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, viz_traj)
    if not viz_traj.exists():
        # Last resort: promote the analysis final snapshot to a single-model trajectory so the
        # 3D viewer always has something to load.
        snapshot = ctx.analysis_dir / "final_snapshot.pdb"
        if snapshot.exists():
            ctx.viz_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(snapshot, viz_traj)
    has_trajectory = viz_traj.exists()
    if not has_trajectory:
        ctx.warning(step, "No trajectory.pdb available for the viewer.")

    # Optional rendered movie (PDR §16.3): rendering a molecular trajectory to mp4 needs BOTH
    # a structural renderer (PyMOL/VMD) to rasterize frames AND ffmpeg to encode them. The base
    # image ships neither by default, so movie rendering is a configured extension, not a
    # default capability. We gate on an explicitly-configured renderer command (MOVIE_RENDER_CMD)
    # and otherwise cleanly skip — the interactive trajectory viewer is the MVP deliverable.
    movie_path = None
    render_cmd = getattr(settings, "movie_render_cmd", None) or os_environ_movie_cmd()
    if render_cmd and has_trajectory:
        movie_path = _run_configured_renderer(ctx, render_cmd, viz_traj)
        if movie_path:
            ctx.info(step, f"Rendered movie via configured renderer: {Path(movie_path).name}.")
        else:
            ctx.warning(step, "Configured movie renderer produced no output; viewer available.")
    else:
        ctx.info(step, "Movie rendering not configured (set MOVIE_RENDER_CMD to enable); "
                       "interactive trajectory viewer is available.")

    n_frames = 0
    if has_trajectory:
        n_frames = max(1, viz_traj.read_text(errors="replace").count("MODEL"))
        # Viewer manifest the frontend 3D viewer reads to configure NGL/Mol*.
        manifest = {
            "trajectory": "trajectory.pdb",
            "format": "pdb",
            "n_frames": n_frames,
            "has_movie": bool(movie_path),
            "movie": Path(movie_path).name if movie_path else None,
            "default_representation": "cartoon+licorice",
            "ligand_resname": "MOL",
        }
        (ctx.viz_dir / "viewer.json").write_text(json.dumps(manifest, indent=2))

    ctx.info(step, f"Visualization ready: trajectory.pdb={'yes' if has_trajectory else 'no'} "
                   f"({n_frames} frames), movie={'yes' if movie_path else 'no'}.")
    return {
        "has_trajectory": has_trajectory,
        "trajectory_path": str(viz_traj) if has_trajectory else None,
        "has_movie": bool(movie_path),
        "movie_path": movie_path,
        "n_frames": n_frames,
    }


def os_environ_movie_cmd():
    import os
    return os.environ.get("MOVIE_RENDER_CMD") or None


def _run_configured_renderer(ctx, render_cmd: str, viz_traj: Path):
    """Run an operator-configured renderer: ``MOVIE_RENDER_CMD`` is a template with {traj} and
    {out} placeholders (e.g. a pymol/vmd+ffmpeg wrapper). Returns the movie path or None.

    This is the single, real extension point for movie rendering — no misleading capability is
    advertised when it is unset. Failures are non-fatal (movie is optional per PDR §16.3).
    """
    import shlex
    import subprocess

    out_path = ctx.viz_dir / "trajectory.mp4"
    try:
        cmd = render_cmd.format(traj=str(viz_traj), out=str(out_path))
        subprocess.run(shlex.split(cmd), check=True, timeout=600,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001 - movie is optional, never abort the step
        ctx.warning("render_movie", f"Movie renderer failed/misconfigured: {exc}")
        return None
    return str(out_path) if out_path.exists() else None
