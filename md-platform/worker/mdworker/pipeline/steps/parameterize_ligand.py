"""Step 5 — parameterize_ligand (CONTRACT §9.5).

small_molecule/cofactor -> acpype GAFF2 + AM1-BCC (split LIG_atomtypes.itp + LIG.itp +
posre); peptide/protein_partner ligand -> pdb2gmx path (amber14sb). The engine encapsulates
the real acpype/pdb2gmx invocation (or synthesizes stubs in the mock engine). This step
resolves the ligand reference structures produced by assign_bond_orders and delegates.
"""

from __future__ import annotations

from typing import Any, Dict


def run(ctx, settings, *, bond_orders: Dict[str, Any], prepared: Dict[str, Any]) -> Dict[str, Any]:
    from mdworker.pipeline.engine import get_engine

    step = "parameterize_ligand"
    lig_ref_sdf = bond_orders.get("lig_ref_sdf")
    ligand_pdb = bond_orders.get("ligand_pdb")
    if not lig_ref_sdf or not ligand_pdb:
        raise ValueError("parameterize_ligand requires assign_bond_orders outputs (lig_ref.sdf, ligand pdb).")

    ligand_type = ctx.ligand_type
    engine = get_engine(settings)
    params = engine.parameterize_ligand(
        ctx, lig_ref_sdf=lig_ref_sdf, ligand_pdb=ligand_pdb, ligand_type=ligand_type
    )
    ctx.info(
        step,
        f"Ligand parameterized via {engine.name} engine "
        f"(ff={params.force_field}, charges={params.charge_method}).",
    )
    return {
        "itp_path": params.itp_path,
        "atomtypes_itp_path": params.atomtypes_itp_path,
        "posre_path": params.posre_path,
        "force_field": params.force_field,
        "charge_method": params.charge_method,
        "engine": engine.name,
        "_params": params,
    }
