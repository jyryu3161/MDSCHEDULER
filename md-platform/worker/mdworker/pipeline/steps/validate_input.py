"""Step 1 — validate_input (CONTRACT §7, §9.1).

This step is import-light orchestration only. The actual work is delegated to focused,
reusable modules:
  - mdworker.io.pdbqt    : PDBQT pose/score parsing + AutoDock element handling (no RDKit)
  - mdworker.io.receptor : PDB/CIF receptor metadata + HETATM classification (no RDKit)
  - mdworker.chem.mapping: RDKit template loading + bond-order transfer (RDKit, lazy)

RDKit is only imported (inside mdworker.chem.mapping) when a chemistry template is present,
so the backend can ``from mdworker.pipeline.steps import validate_input`` and run cheap
parsing/classification without paying the RDKit import cost.

The single atom-mapping implementation lives in mdworker.chem.mapping and backs BOTH this
feasibility check and the heavy assign_bond_orders step, so the two paths cannot diverge.
The chemistry/template is GENERAL — the 3-HDC C23H40O2 graph is never hardcoded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from mdworker.io.pdbqt import (
    heavy_element_counts,
    parse_pdbqt_models,
)
from mdworker.io.receptor import parse_receptor, suggest_hetatm


def classify_chem_source(
    *,
    chemistry_file: Optional[str],
    smiles: Optional[str],
    meeko_mapping: bool,
) -> str:
    """Return chem_source token: sdf|mol2|smiles|meeko|none (CONTRACT §7)."""
    if chemistry_file:
        suffix = Path(chemistry_file).suffix.lower()
        if suffix in (".sdf", ".mol", ".sd"):
            return "sdf"
        if suffix == ".mol2":
            return "mol2"
    if smiles:
        return "smiles"
    if meeko_mapping:
        return "meeko"
    return "none"


def _ligand_type_candidates(
    poses_parsed: List[Dict[str, Any]], ligand_type: Optional[str]
) -> List[str]:
    if ligand_type and ligand_type != "unknown":
        return [ligand_type]
    if not poses_parsed:
        return ["unknown"]
    heavy_n = len(poses_parsed[0]["heavy_atoms"])
    counts = heavy_element_counts(poses_parsed[0])
    n_count = counts.get("N", 0)
    if heavy_n <= 80 and n_count <= max(2, heavy_n // 6):
        return ["small_molecule"]
    if heavy_n > 80:
        return ["peptide", "protein_partner"]
    return ["small_molecule", "peptide"]


def _empty_report(input_type: str, message: str, *, errors: List[str]) -> Dict[str, Any]:
    return {
        "ok": False,
        "input_type": input_type,
        "pose_count": 0,
        "poses": [],
        "ligand_type_candidates": [],
        "chem_source": "none",
        "atom_mapping": {"attempted": False, "success": False, "message": message},
        "hetatm_candidates": [],
        "receptor": None,
        "errors": errors,
        "warnings": [],
    }


def validate_input(
    *,
    pose_file: str,
    chemistry_file: Optional[str] = None,
    receptor_file: Optional[str] = None,
    smiles: Optional[str] = None,
    meeko_mapping: bool = False,
    ligand_type: Optional[str] = None,
    require_ligand_chemistry: bool = True,
    allow_smiles_input: bool = True,
    allow_meeko_mapping_input: bool = True,
) -> Dict[str, Any]:
    """Validate inputs and produce the ValidationReport (CONTRACT §7).

    Import-light public function reused by the backend upload-validate route and the worker
    pipeline. RDKit is only imported when a chemistry template is present.
    """
    errors: List[str] = []
    warnings: List[str] = []

    # ----- parse poses (no RDKit) -----------------------------------------------------
    try:
        poses_parsed = parse_pdbqt_models(pose_file)
    except FileNotFoundError:
        return _empty_report("pdbqt", "Pose file not found.", errors=[f"Pose file not found: {pose_file}"])
    except Exception as exc:  # noqa: BLE001
        return _empty_report("pdbqt", f"Pose parse error: {exc}", errors=[f"Could not parse pose file: {exc}"])

    pose_count = len(poses_parsed)
    if pose_count == 0:
        errors.append("No poses (no ATOM/HETATM records) found in the pose file.")

    poses_out = [
        {"index": p["index"], "docking_score": p["docking_score"]} for p in poses_parsed
    ]

    # ----- input type classification --------------------------------------------------
    pose_suffix = Path(pose_file).suffix.lower()
    receptor_present = bool(receptor_file)
    if pose_suffix == ".pdbqt":
        input_type = "mixed" if receptor_present else "pdbqt"
    elif pose_suffix in (".cif", ".mmcif"):
        input_type = "cif"
    elif pose_suffix == ".pdb":
        input_type = "pdb"
    else:
        input_type = "pdbqt"

    # ----- chem source ----------------------------------------------------------------
    chem_source = classify_chem_source(
        chemistry_file=chemistry_file, smiles=smiles, meeko_mapping=meeko_mapping
    )

    ligand_type_candidates = _ligand_type_candidates(poses_parsed, ligand_type)

    # ----- atom mapping (delegates to the single shared implementation) ---------------
    if chem_source in ("sdf", "mol2", "smiles") and poses_parsed:
        try:
            from mdworker.chem.mapping import attempt_atom_mapping  # lazy RDKit import

            atom_mapping = attempt_atom_mapping(
                poses_parsed[0],
                chemistry_file=chemistry_file,
                smiles=smiles,
                chem_source=chem_source,
            )
        except ImportError as exc:
            atom_mapping = {
                "attempted": False,
                "success": False,
                "message": f"RDKit unavailable for atom mapping: {exc}",
            }
            warnings.append("RDKit not available; atom-mapping feasibility not checked.")
        except Exception as exc:  # noqa: BLE001
            atom_mapping = {
                "attempted": True,
                "success": False,
                "message": f"Atom-mapping check error: {exc}",
            }
    elif chem_source == "meeko":
        atom_mapping = {
            "attempted": True,
            "success": bool(allow_meeko_mapping_input),
            "message": (
                "Meeko mapping supplied; bond orders taken from Meeko atom typing."
                if allow_meeko_mapping_input
                else "Meeko mapping input is disabled (ALLOW_MEEKO_MAPPING_INPUT=false)."
            ),
        }
    else:
        atom_mapping = {
            "attempted": False,
            "success": False,
            "message": "No chemistry template supplied; bond orders cannot be perceived from raw PDBQT.",
        }

    # ----- receptor metadata ----------------------------------------------------------
    receptor_info = None
    hetatm_candidates: List[Dict[str, Any]] = []
    if receptor_file:
        try:
            rec, rec_warnings = parse_receptor(receptor_file)
            warnings.extend(rec_warnings)
            receptor_info = {
                "format": rec["format"],
                "chains": rec["chains"],
                "n_residues": rec["n_residues"],
                "n_atoms": rec["n_atoms"],
                "has_hetatm": rec["has_hetatm"],
            }
            for resname, count in sorted(rec["hetatm_resnames"].items()):
                hetatm_candidates.append(
                    {"resname": resname, "count": count, "suggested": suggest_hetatm(resname)}
                )
        except FileNotFoundError:
            warnings.append(f"Receptor file not found: {receptor_file}")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not parse receptor file: {exc}")

    # ----- hard-rule evaluation (CONTRACT §7) -----------------------------------------
    has_chemistry = chem_source in ("sdf", "mol2", "smiles") or (
        chem_source == "meeko" and allow_meeko_mapping_input
    )
    if require_ligand_chemistry and not has_chemistry:
        errors.append(
            "Raw PDBQT supplied without ligand chemistry (SDF/MOL2/SMILES/Meeko). "
            "Bond orders cannot be perceived from PDBQT alone."
        )
    if chem_source == "smiles" and not allow_smiles_input:
        errors.append("SMILES input is disabled (ALLOW_SMILES_INPUT=false).")
    if atom_mapping.get("attempted") and not atom_mapping.get("success"):
        msg = atom_mapping.get("message", "")
        if "formula" in msg.lower():
            errors.append("Ligand chemistry does not match poses: " + msg)
        else:
            errors.append("Atom mapping failed: " + msg)

    ok = len(errors) == 0

    return {
        "ok": ok,
        "input_type": input_type,
        "pose_count": pose_count,
        "poses": poses_out,
        "ligand_type_candidates": ligand_type_candidates,
        "chem_source": chem_source,
        "atom_mapping": atom_mapping,
        "hetatm_candidates": hetatm_candidates,
        "receptor": receptor_info,
        "errors": errors,
        "warnings": warnings,
    }


# Pipeline-step entry point (used by runner). Writes the report to disk for the job dir.
def run(ctx, settings) -> Dict[str, Any]:
    """Pipeline step wrapper: re-validate within the job context and persist the report."""
    step = "validate_input"
    ctx.set_status("validating", current_step=step, progress=2.0)
    ctx.info(step, "Validating inputs and chemistry mapping.")

    meta = ctx.job_meta
    inputs = meta.get("inputs", {})
    pose_file = inputs.get("pose_file")
    chemistry_file = inputs.get("chemistry_file")
    receptor_file = inputs.get("receptor_file")
    smiles = inputs.get("smiles") or meta.get("smiles")

    report = validate_input(
        pose_file=pose_file,
        chemistry_file=chemistry_file,
        receptor_file=receptor_file,
        smiles=smiles,
        meeko_mapping=bool(inputs.get("meeko_mapping")),
        ligand_type=meta.get("ligand_type"),
        require_ligand_chemistry=settings.require_ligand_chemistry,
        allow_smiles_input=settings.allow_smiles_input,
        allow_meeko_mapping_input=settings.allow_meeko_mapping_input,
    )

    ctx.input_processed_dir.mkdir(parents=True, exist_ok=True)
    (ctx.input_processed_dir / "validation_report.json").write_text(json.dumps(report, indent=2))

    if not report["ok"]:
        raise ValueError("Input validation failed: " + "; ".join(report.get("errors", [])))
    ctx.info(
        step,
        f"Validation passed: {report['pose_count']} pose(s), chem_source={report['chem_source']}.",
    )
    return report
