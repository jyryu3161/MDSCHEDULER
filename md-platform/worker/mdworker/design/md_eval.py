"""MD refinement of a docked peptide–compound complex → binding ΔG (kcal/mol).

This is the expensive arm of the design hybrid: only the per-generation docking elites reach
it. Two backends:

  • ``mock``    — a fast, deterministic ΔG anchored to the docking score (ΔG ≈ k·dock − c).
                 No GROMACS needed; lets the whole GA / web / UI path be exercised quickly and
                 makes the small-scale functional test tractable.
  • ``gromacs`` — a real short MD (the proven engine: solvate → EM → NVT/NPT → production) on
                 the docked complex, then MM/GBSA over the bound window → ΔG. Falls back to the
                 mock estimate (with a logged reason) if GROMACS/AmberTools are unavailable, so
                 a design run never hard-fails on a missing tool.

``evaluate`` returns a float ΔG where MORE NEGATIVE = STRONGER binding (the GA maximizes −ΔG).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Optional

# Empirical anchor: MM/GBSA ΔG for these systems runs ~1.6× the Vina score and a touch
# stronger; deterministic so the mock GA is reproducible.
_MOCK_SLOPE = 1.6
_MOCK_OFFSET = 1.0


def synthetic_dg(docking_score: float) -> float:
    """Deterministic mock ΔG anchored to the docking score (kcal/mol, negative = stronger)."""
    return round(_MOCK_SLOPE * float(docking_score) - _MOCK_OFFSET, 3)


def gromacs_available() -> bool:
    return shutil.which("gmx") is not None or shutil.which("gmx_mpi") is not None


def evaluate(
    sequence: str,
    docking_score: float,
    *,
    engine: str = "mock",
    workdir: Optional[Path] = None,
    peptide_pdb: Optional[str] = None,
    pose_pdbqt: Optional[str] = None,
    gpu_id: Optional[int] = None,
    md_length_ns: float = 10.0,
    settings: Optional[dict] = None,
    log: Optional[Callable[[str], None]] = None,
) -> float:
    """Return the binding ΔG (kcal/mol) for one docked candidate."""
    def _log(msg: str) -> None:
        if log:
            log(msg)

    if engine != "gromacs":
        return synthetic_dg(docking_score)

    if not gromacs_available():
        _log(f"GROMACS not available; using mock ΔG for {sequence}.")
        return synthetic_dg(docking_score)
    try:
        if workdir:
            return _gromacs_dg(
                sequence, workdir=Path(workdir), peptide_pdb=peptide_pdb, pose_pdbqt=pose_pdbqt,
                gpu_id=gpu_id, md_length_ns=md_length_ns, settings=settings or {}, log=_log,
            )
        # No caller workdir: use a self-cleaning temp dir so MD artifacts don't accumulate.
        import tempfile

        with tempfile.TemporaryDirectory(prefix="design_md_") as td:
            return _gromacs_dg(
                sequence, workdir=Path(td), peptide_pdb=peptide_pdb, pose_pdbqt=pose_pdbqt,
                gpu_id=gpu_id, md_length_ns=md_length_ns, settings=settings or {}, log=_log,
            )
    except Exception as exc:  # noqa: BLE001 — never let one candidate kill the run
        _log(f"Real MD evaluation failed for {sequence} ({exc!r}); falling back to mock ΔG.")
        return synthetic_dg(docking_score)


def evaluate_replicas(
    sequence: str,
    docking_score: float,
    *,
    n_replicas: int = 1,
    engine: str = "mock",
    workdir: Optional[Path] = None,
    log: Optional[Callable[[str], None]] = None,
    **kw,
) -> dict:
    """Run ``n_replicas`` MD evaluations and aggregate to mean ± SEM.

    Each replica gets its own ``workdir/rep_RR`` so trajectories never collide. Real-MD replicas
    are independent because the NVT step generates velocities with ``gen_seed = -1`` (GROMACS
    draws a fresh random seed each run); the mock ΔG is deterministic, so its replicas are
    identical (SEM 0), which is correct for the mock path. Returns
    {"dg": mean, "sem": ..., "std": ..., "n": k, "values": [...]}; the GA uses ``dg`` as fitness.
    """
    n = max(1, int(n_replicas or 1))
    values: list[float] = []
    for r in range(1, n + 1):
        wd = (Path(workdir) / f"rep_{r:02d}") if workdir else None
        values.append(float(evaluate(sequence, docking_score, engine=engine, workdir=wd,
                                     log=log, **kw)))
    mean = sum(values) / len(values)
    if len(values) > 1:
        var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        std = var ** 0.5
        sem = std / (len(values) ** 0.5)
    else:
        std = sem = 0.0
    if log and n > 1:
        log(f"{sequence}: ΔG {mean:.2f} ± {sem:.2f} kcal/mol over {n} replicas "
            f"(values {[round(v, 2) for v in values]}).")
    return {"dg": round(mean, 3), "sem": round(sem, 3), "std": round(std, 3),
            "n": len(values), "values": [round(v, 3) for v in values]}


def _gromacs_dg(sequence, *, workdir, peptide_pdb, pose_pdbqt, gpu_id, md_length_ns, settings, log) -> float:
    """Real short MD + MM/GBSA on the docked complex via the proven design MD adapter.

    Delegates to mdworker.design.complex_md, which assembles the peptide + docked compound,
    runs the GROMACS engine for ``md_length_ns`` and computes MM/GBSA ΔG over the bound window.
    """
    from .complex_md import run_complex_md  # local import: heavy deps only on the real path

    dg = run_complex_md(
        sequence=sequence, workdir=Path(workdir), peptide_pdb=peptide_pdb, pose_pdbqt=pose_pdbqt,
        gpu_id=gpu_id, md_length_ns=md_length_ns, settings=settings, log=log,
    )
    return float(dg)
