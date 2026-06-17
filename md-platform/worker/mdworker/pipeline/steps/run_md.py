"""Step 6 — run_md (CONTRACT §9.6).

Assemble complex (receptor + ligand coords) -> box (BOX_PADDING_NM, default 1.2 nm) ->
solvate -> genion (salt_concentration NaCl) -> EM -> NVT (NVT_STEPS, default 100 ps) ->
NPT (NPT_STEPS, default 250 ps) -> production MD (md_length_ns). The protein force field +
water model are resolved by the engine (ff19SB/OPC by default, with preflight fallback to
amber14sb/tip3p). Status transitions preparing -> running_em -> running_nvt -> running_npt ->
running_md are emitted by the engine, which also reports ns/day + completed_ns + progress.
The engine is chosen by MD_ENGINE/auto (gromacs vs mock).

GPU note: the GPU lock is requested by the runner BEFORE this step and passed in via
assigned_gpu; the engine sets CUDA_VISIBLE_DEVICES for real gmx mdrun.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def run(
    ctx,
    settings,
    *,
    prepared: Dict[str, Any],
    ligand: Dict[str, Any],
    bond_orders: Dict[str, Any],
    assigned_gpu: Optional[int],
) -> Dict[str, Any]:
    from mdworker.pipeline.engine import get_engine

    step = "run_md"
    engine = get_engine(settings)

    prepared_obj = prepared.get("_prepared")
    ligand_obj = ligand.get("_params")
    if prepared_obj is None or ligand_obj is None:
        raise ValueError("run_md requires prepared structure + ligand parameters from prior steps.")

    ligand_pdb = bond_orders.get("ligand_pdb")
    md_length_ns = ctx.md_length_ns

    ctx.info(
        step,
        f"Starting MD ({engine.name} engine): {md_length_ns:g} ns, "
        f"GPU={'none' if assigned_gpu is None else assigned_gpu}.",
    )

    result = engine.run_md(
        ctx,
        prepared=prepared_obj,
        ligand=ligand_obj,
        ligand_pdb=ligand_pdb,
        md_length_ns=md_length_ns,
        assigned_gpu=assigned_gpu,
    )

    # Final production status snapshot.
    ctx.set_status(
        "running_md",
        current_step=step,
        progress=85.0,
        completed_ns=result.completed_ns,
        ns_per_day=result.ns_per_day,
    )
    ctx.info(
        step,
        f"MD complete: {result.completed_ns:g} ns, {result.n_frames} trajectory frames, "
        f"{result.ns_per_day:g} ns/day.",
    )
    return {
        "trajectory_pdb_path": result.trajectory_pdb_path,
        "final_gro_path": result.final_gro_path,
        "xtc_path": result.xtc_path,
        "tpr_path": result.tpr_path,
        "completed_ns": result.completed_ns,
        "ns_per_day": result.ns_per_day,
        "n_frames": result.n_frames,
        "frame_interval_ps": result.frame_interval_ps,
        "engine": engine.name,
    }
