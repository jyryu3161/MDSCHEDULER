"""Step 3 — assign_bond_orders (CONTRACT §9.3).

GENERAL bond-order assignment from the chemistry template to this pose's heavy-atom
coordinates, then AddHs(addCoords=True). Writes pose_N_lig.pdb (full-atom ligand for the
pose) + lig_ref.sdf (a clean reference conformer used for parameterization). Rejects on
mapping failure with a clear error.

This is the heavy counterpart of validate_input's feasibility check; both share the single
implementation in mdworker.chem.mapping, so a pose that passed validation will succeed here.
The 3-HDC C23H40O2 graph is NEVER hardcoded — the template comes from the supplied
SDF / MOL2 / SMILES.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional

from mdworker.io.pdbqt import parse_pdbqt_models
from mdworker.pipeline.steps.validate_input import classify_chem_source


def _extract_meeko_smiles(pose_file: str) -> Optional[str]:
    """Extract the ligand SMILES Meeko embeds in PDBQT REMARK records.

    Meeko writes lines such as ``REMARK SMILES CCCC...`` (and ``REMARK SMILES IDX ...``
    index maps, which are skipped here). Returns the first SMILES string found, or None.
    """
    try:
        text = Path(pose_file).read_text(errors="replace")
    except OSError:
        return None
    for ln in text.splitlines():
        s = ln.strip()
        if not s.startswith("REMARK"):
            continue
        # Match 'REMARK SMILES <smiles>' but not 'REMARK SMILES IDX ...'.
        m = re.match(r"REMARK\s+SMILES\s+(?!IDX\b)(\S+)", s)
        if m:
            candidate = m.group(1).strip()
            if candidate:
                return candidate
    return None


def run(ctx, settings) -> Dict[str, Any]:
    from rdkit import Chem  # lazy
    from rdkit.Chem import AllChem
    from rdkit.Chem import rdMolDescriptors as desc

    from mdworker.chem.mapping import (
        load_template_mol,
        map_pose_to_template,
        template_formula,
    )

    step = "assign_bond_orders"
    ctx.set_status("preparing", current_step=step, progress=9.0)

    inputs = ctx.job_meta.get("inputs", {})
    chemistry_file = inputs.get("chemistry_file")
    smiles = inputs.get("smiles") or ctx.job_meta.get("smiles")
    chem_source = classify_chem_source(
        chemistry_file=chemistry_file, smiles=smiles, meeko_mapping=bool(inputs.get("meeko_mapping"))
    )

    pose_file = inputs.get("pose_file")

    # Meeko-prepared PDBQT carries a SMILES (REMARK SMILES / REMARK 'Meeko') describing the
    # ligand chemistry. validate_input accepts that path when ALLOW_MEEKO_MAPPING_INPUT is
    # set, so honor it here by extracting the embedded SMILES and using it as the template,
    # keeping both stages aligned.
    if chem_source == "meeko":
        if not settings.allow_meeko_mapping_input:
            raise ValueError("Meeko mapping input is disabled (ALLOW_MEEKO_MAPPING_INPUT=false).")
        meeko_smiles = _extract_meeko_smiles(pose_file)
        if not meeko_smiles:
            raise ValueError(
                "Meeko mapping selected but no SMILES found in the PDBQT REMARK records; "
                "cannot reconstruct ligand bond orders."
            )
        smiles = meeko_smiles
        chem_source = "smiles"
        ctx.info(step, f"Using Meeko-embedded SMILES for bond orders: {smiles}")

    if chem_source not in ("sdf", "mol2", "smiles"):
        raise ValueError(
            "assign_bond_orders requires a chemistry template (SDF/MOL2/SMILES/Meeko); "
            f"chem_source={chem_source}. Bond orders cannot be perceived from raw PDBQT."
        )

    # Resolve this pose from the original pose file.
    poses = parse_pdbqt_models(pose_file)
    target = next((p for p in poses if p["index"] == ctx.pose_index), None)
    if target is None and 1 <= ctx.pose_index <= len(poses):
        target = poses[ctx.pose_index - 1]
    if target is None:
        raise ValueError(f"Pose index {ctx.pose_index} not found in {pose_file}.")

    template, err = load_template_mol(
        chemistry_file=chemistry_file, smiles=smiles, chem_source=chem_source
    )
    if template is None:
        raise ValueError(f"Failed to load chemistry template: {err}")
    tmpl_formula = template_formula(template)

    mapped_h, formula, err = map_pose_to_template(target, template=template)
    if mapped_h is None:
        raise ValueError(
            f"Bond-order assignment failed for pose {ctx.pose_index}: {err}"
        )
    if tmpl_formula and formula != tmpl_formula:
        raise ValueError(
            f"Pose {ctx.pose_index} chemistry mismatch after mapping: "
            f"template {tmpl_formula} vs pose {formula}."
        )

    # Write full-atom ligand PDB for this pose.
    lig_pdb = ctx.prep_dir / f"pose_{ctx.pose_index}_lig.pdb"
    Chem.MolToPDBFile(mapped_h, str(lig_pdb))

    # Write a clean reference conformer (for parameterization) — embed + MMFF optimize a
    # hydrogen-complete copy of the template (matches preprocess_pipeline.sh build_ligand).
    lig_ref = ctx.prep_dir / "lig_ref.sdf"
    ref = Chem.AddHs(Chem.Mol(template))
    embed_status = AllChem.EmbedMolecule(ref, randomSeed=1)
    if embed_status == 0:
        try:
            AllChem.MMFFOptimizeMolecule(ref)
        except Exception:  # noqa: BLE001 - optimization is best-effort
            pass
    else:
        # Embedding failed (rare); fall back to the mapped pose conformer as the reference.
        ref = mapped_h
    Chem.MolToMolFile(ref, str(lig_ref))

    n_atoms = mapped_h.GetNumAtoms()
    n_heavy = sum(1 for a in mapped_h.GetAtoms() if a.GetSymbol() != "H")
    ctx.info(
        step,
        f"Assigned bond orders for pose {ctx.pose_index}: formula {formula}, "
        f"{n_heavy} heavy + {n_atoms - n_heavy} H = {n_atoms} atoms. "
        f"Wrote {lig_pdb.name} + {lig_ref.name}.",
    )
    return {
        "ligand_pdb": str(lig_pdb),
        "lig_ref_sdf": str(lig_ref),
        "molformula": formula,
        "n_atoms": n_atoms,
        "n_heavy": n_heavy,
    }
