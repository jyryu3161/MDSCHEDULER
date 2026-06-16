"""Real short MD + MM/GBSA on a docked peptide–compound complex (design MD refinement).

Reuses the PROVEN production engine (mdworker.pipeline.engine.gromacs) and steps rather than
reimplementing MD: prepare_structure (peptide receptor) → parameterize_ligand (compound) →
run_md → analyze_md (bound-window detection) → mmpbsa (MM/GBSA ΔG over the bound window). The
same code path validated on the 50 ns KCCIVYP+3-HDC run. Returns the MM/GBSA ΔG (kcal/mol).

Heavy: one call is a full solvated MD (~minutes). Invoked only for the per-generation docking
elites. Raises on failure so the caller (md_eval) can fall back to the mock estimate.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional


class _LocalReporter:
    """No-network Reporter for in-process design MD: logs via a callback, never cancels."""

    def __init__(self, log: Optional[Callable[[str], None]] = None):
        self._log = log

    def set_subjob_status(self, *a, **k) -> None: ...
    def set_job_status(self, *a, **k) -> None: ...
    def request_gpu(self, *a, **k):  # GPU is pre-assigned by the design runner
        return None
    def release_gpu(self, *a, **k) -> None: ...
    def is_cancelled(self, *a, **k) -> bool:
        return False

    def log(self, job_id, subjob_id, level, step, message) -> None:
        if self._log:
            self._log(f"[{level}/{step}] {message}")


def _pose_to_pdb(pose_pdbqt: Path, out_pdb: Path) -> Path:
    """Convert the top docked pose (PDBQT) to PDB for ligand parameterization."""
    obabel = shutil.which("obabel")
    if not obabel:
        raise RuntimeError("obabel not on PATH; cannot convert docked pose to PDB.")
    out_pdb.parent.mkdir(parents=True, exist_ok=True)
    if out_pdb.exists():
        out_pdb.unlink()  # never mistake a stale file for a successful retry
    # -f 1 -l 1 = first model only (the best-scoring pose).
    proc = subprocess.run([obabel, str(pose_pdbqt), "-O", str(out_pdb), "-f", "1", "-l", "1"],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not out_pdb.exists() or out_pdb.stat().st_size == 0:
        raise RuntimeError(f"obabel pose->pdb failed (rc={proc.returncode}): "
                           f"{(proc.stderr or proc.stdout)[-300:]}")
    return out_pdb


def run_complex_md(
    *,
    sequence: str,
    workdir: Path,
    peptide_pdb: Optional[str],
    pose_pdbqt: Optional[str],
    compound_sdf: Optional[str] = None,
    gpu_id: Optional[int] = None,
    md_length_ns: float = 10.0,
    settings: Optional[dict] = None,
    log: Optional[Callable[[str], None]] = None,
) -> float:
    """Run real MD + MM/GBSA on the docked complex; return ΔG (kcal/mol, negative = stronger)."""
    from mdworker.config import load_settings
    from mdworker.pipeline.context import JobContext
    from mdworker.pipeline.engine.gromacs import GromacsEngine
    from mdworker.pipeline.steps import analyze_md, mmpbsa

    if not workdir:
        raise ValueError("workdir is required for the GROMACS design MD path.")
    if not peptide_pdb or not pose_pdbqt:
        raise ValueError("peptide_pdb and pose_pdbqt are required for real MD evaluation.")
    settings = settings or {}

    # Worker Settings with the real engine forced; honor template dir / output spacing.
    st = load_settings()
    st.md_engine = "gromacs"
    if settings.get("MDP_TEMPLATE_DIR"):
        st.mdp_template_dir = settings["MDP_TEMPLATE_DIR"]
    if settings.get("TRAJECTORY_OUTPUT_PS"):
        st.trajectory_output_ps = int(settings["TRAJECTORY_OUTPUT_PS"])
    # The engine reads box/temperature/pressure/salt from settings.extra during run_md; propagate
    # the caller's values so MD ionization and the MM/GBSA salt term agree.
    salt = float(settings.get("salt_concentration", 0.15))
    st.extra = dict(st.extra or {})
    st.extra.setdefault("box_type", settings.get("box_type", "dodecahedron"))
    st.extra.setdefault("temperature", settings.get("temperature", 300))
    st.extra.setdefault("pressure", settings.get("pressure", 1.0))
    st.extra["salt_concentration"] = salt

    workdir = Path(workdir)
    reporter = _LocalReporter(log)
    ctx = JobContext(
        job_id=f"design_md_{sequence}", subjob_id=f"design_md_{sequence}", pose_index=1,
        storage_root=str(workdir), reporter=reporter,
        job_meta={"compute_mmpbsa": True, "salt_concentration": settings.get("salt_concentration", 0.15)},
        subjob_meta={},
    )
    ctx.ensure_dirs()

    engine = GromacsEngine(st)
    ligand_pdb = _pose_to_pdb(Path(pose_pdbqt), ctx.prep_dir / "ligand_pose.pdb")
    lig_ref = compound_sdf or settings.get("compound_file")
    if not lig_ref:
        raise ValueError("compound_sdf/compound_file required to parameterize the ligand.")

    prepared = engine.prepare_structure(ctx, receptor_file=str(peptide_pdb), hetatm_decisions={})
    ligand = engine.parameterize_ligand(ctx, lig_ref_sdf=str(lig_ref), ligand_pdb=str(ligand_pdb),
                                        ligand_type="small_molecule")
    md = engine.run_md(ctx, prepared=prepared, ligand=ligand, ligand_pdb=str(ligand_pdb),
                       md_length_ns=float(md_length_ns), assigned_gpu=gpu_id)
    md_dict = {
        "trajectory_pdb_path": md.trajectory_pdb_path, "final_gro_path": md.final_gro_path,
        "xtc_path": md.xtc_path, "tpr_path": md.tpr_path, "completed_ns": md.completed_ns,
        "n_frames": md.n_frames, "frame_interval_ps": md.frame_interval_ps, "engine": "gromacs",
    }
    # analyze_md writes the bound window summary that mmpbsa reads to scope the ΔG.
    analyze_md.run(ctx, st, md=md_dict)
    res = mmpbsa.run(ctx, st, md=md_dict)
    dg = res.get("gbsa_dg_kcal_mol")
    if dg is None:
        dg = res.get("pbsa_dg_kcal_mol")
    if dg is None:
        raise RuntimeError(f"MM/GBSA produced no ΔG for {sequence} (skipped: {res.get('reason')}).")
    return float(dg)
