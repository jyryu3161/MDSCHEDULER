"""GENERAL atom-mapping: transfer bond orders from a chemistry template to each pose.

This is the generalization of the proven recipe in preprocess_pipeline.sh `build_ligand`
(which hardcoded the 3-HDC C23H40O2 graph). Here the bond graph / chemistry comes from a
user-supplied SDF / MOL2 / SMILES template — never hardcoded.

Approach (per CONTRACT §9.3):
  1. Load the chemistry template as an RDKit Mol (heavy atoms, full bond orders).
  2. Build a heavy-atom-only pose Mol from the PDBQT coordinates and perceive connectivity
     via rdDetermineBonds.DetermineConnectivity (no bond orders yet).
  3. AllChem.AssignBondOrdersFromTemplate(template_without_Hs, pose) transfers bond orders
     onto the pose (matching by graph isomorphism).
  4. Sanitize, then AddHs(addCoords=True) places hydrogens geometrically.
  5. Verify the resulting molecular formula equals the template's.

The same routine backs both the cheap validate feasibility check and the heavy
assign_bond_orders step, so success in validation guarantees the heavy step proceeds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def load_template_mol(
    *,
    chemistry_file: Optional[str],
    smiles: Optional[str],
    chem_source: str,
):
    """Load an RDKit Mol from SDF/MOL2/SMILES.

    Returns (mol_without_Hs, error_or_None). The returned mol carries full bond orders with
    explicit Hs removed, suitable as the template for AssignBondOrdersFromTemplate.
    """
    from rdkit import Chem  # lazy

    mol = None
    try:
        if chem_source == "smiles" and smiles:
            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                return None, f"RDKit could not parse SMILES: {smiles!r}"
        elif chemistry_file:
            p = Path(chemistry_file)
            suffix = p.suffix.lower()
            if suffix in (".sdf", ".mol", ".sd"):
                mol = _read_first_sdf(str(p))
                if mol is None:
                    return None, f"RDKit could not read any molecule from SDF: {p.name}"
            elif suffix == ".mol2":
                mol = Chem.MolFromMol2File(str(p), removeHs=False, sanitize=True)
                if mol is None:
                    mol = Chem.MolFromMol2File(str(p), removeHs=False, sanitize=False)
                    if mol is not None:
                        _safe_sanitize(mol)
                if mol is None:
                    return None, f"RDKit could not read MOL2: {p.name}"
            else:
                return None, f"Unsupported chemistry file extension: {suffix}"
        else:
            return None, "No chemistry template provided."
    except Exception as exc:  # noqa: BLE001
        return None, f"Error loading chemistry template: {exc}"

    if mol is None:
        return None, "Chemistry template could not be loaded."

    try:
        mol_noh = Chem.RemoveHs(mol)
    except Exception:  # noqa: BLE001
        mol_noh = mol
    return mol_noh, None


def _read_first_sdf(path: str):
    from rdkit import Chem  # lazy

    supplier = Chem.SDMolSupplier(path, removeHs=False, sanitize=True)
    for m in supplier:
        if m is not None:
            return m
    # Retry tolerant of imperfect valence/aromaticity flags.
    supplier = Chem.SDMolSupplier(path, removeHs=False, sanitize=False)
    for m in supplier:
        if m is not None:
            _safe_sanitize(m)
            return m
    return None


def _safe_sanitize(mol) -> None:
    from rdkit import Chem  # lazy

    try:
        Chem.SanitizeMol(mol)
    except Exception:  # noqa: BLE001
        pass


def build_pose_heavy_mol(pose: Dict[str, Any]):
    """Build an RDKit Mol of heavy atoms with perceived connectivity (no bond orders set).

    Returns (mol, error_or_None). Implicit hydrogens are left enabled so that, after the
    template assigns bond orders, sanitization can recompute the correct H count.
    """
    from rdkit import Chem  # lazy
    from rdkit.Chem import rdDetermineBonds
    from rdkit.Geometry import Point3D

    heavy = pose["heavy_atoms"]
    if not heavy:
        return None, "Pose has no heavy atoms."

    rw = Chem.RWMol()
    conf = Chem.Conformer(len(heavy))
    for i, a in enumerate(heavy):
        atom = Chem.Atom(a.element)
        rw.AddAtom(atom)
        conf.SetAtomPosition(i, Point3D(float(a.x), float(a.y), float(a.z)))
    mol = rw.GetMol()
    mol.AddConformer(conf, assignId=True)

    try:
        rdDetermineBonds.DetermineConnectivity(mol)
    except Exception as exc:  # noqa: BLE001
        return None, f"DetermineConnectivity failed: {exc}"
    return mol, None


def map_pose_to_template(
    pose: Dict[str, Any],
    *,
    template,
):
    """Transfer bond orders from a loaded template onto one pose's heavy-atom skeleton.

    Returns (mol_with_Hs, formula, error_or_None). The returned mol has bond orders from the
    template and hydrogens placed geometrically (AddHs addCoords=True).
    """
    from rdkit import Chem  # lazy
    from rdkit.Chem import AllChem
    from rdkit.Chem import rdMolDescriptors as desc

    template_heavy = template.GetNumAtoms()
    pose_mol, err = build_pose_heavy_mol(pose)
    if pose_mol is None:
        return None, None, err

    if pose_mol.GetNumAtoms() != template_heavy:
        return (
            None,
            None,
            f"Heavy-atom count mismatch: template has {template_heavy}, "
            f"pose has {pose_mol.GetNumAtoms()}.",
        )

    try:
        mapped = AllChem.AssignBondOrdersFromTemplate(template, pose_mol)
    except Exception as exc:  # noqa: BLE001
        return None, None, f"AssignBondOrdersFromTemplate failed: {exc}"

    # rdDetermineBonds.DetermineConnectivity marks every atom noImplicit=True and can leave
    # radical electrons that absorb the open valence; AssignBondOrdersFromTemplate carries
    # those flags through. To let RDKit reconstruct the correct hydrogen complement from the
    # now-correct bond orders, clear those flags, then re-sanitize so implicit H counts are
    # recomputed from valence, then place explicit hydrogens geometrically.
    try:
        rw = Chem.RWMol(mapped)
        for atom in rw.GetAtoms():
            atom.SetNoImplicit(False)
            atom.SetNumRadicalElectrons(0)
            atom.SetNumExplicitHs(0)
        mapped = rw.GetMol()
        Chem.SanitizeMol(mapped)
    except Exception as exc:  # noqa: BLE001
        return None, None, f"Mapped pose failed sanitization: {exc}"

    mapped_h = Chem.AddHs(mapped, addCoords=True)
    formula = desc.CalcMolFormula(mapped_h)

    # Robustness fallback: DetermineConnectivity can over-bond folded poses, which makes the
    # template substructure match (and thus the H count / formula) wrong for some molecules.
    # When the formula does not match the template, retry with a conservative distance graph +
    # element-typed VF2 isomorphism grafting. Because coordinates are grafted ONTO the trusted
    # template (which already carries correct bond orders), any valid element+connectivity
    # isomorphism yields the correct topology; where multiple isomorphisms exist they are graph
    # automorphisms over chemically-equivalent atoms, so the only ambiguity is which of two
    # equivalent atoms receives which coordinate — chemically identical and acceptable as an MD
    # starting structure. The formula re-check below rejects any non-equivalent mismatch.
    tmpl_formula = template_formula(template)
    if tmpl_formula and formula != tmpl_formula:
        vf2_h, vf2_formula, vf2_err = _vf2_graft(pose, template=template)
        if vf2_h is not None and vf2_formula == tmpl_formula:
            return vf2_h, vf2_formula, None
    return mapped_h, formula, None


def _vf2_graft(pose: Dict[str, Any], *, template):
    """Graft pose coordinates onto the trusted template via element-typed VF2 isomorphism.

    Builds a heavy-atom connectivity graph for the pose with a conservative covalent-radius
    cutoff and finds an element-matched subgraph isomorphism to the template's heavy-atom
    graph (networkx VF2). The template (which already carries correct bond orders + implicit
    H counts) is copied and its conformer set to the mapped pose coordinates, then hydrogens
    are placed geometrically. Returns (mol_with_Hs, formula, error_or_None).
    """
    try:
        import numpy as np
        import networkx as nx
        from networkx.algorithms import isomorphism
        from rdkit import Chem
        from rdkit.Chem import rdMolDescriptors as desc
        from rdkit.Geometry import Point3D
    except Exception as exc:  # noqa: BLE001
        return None, None, f"VF2 fallback unavailable: {exc}"

    cov = {
        "H": 0.31, "B": 0.84, "C": 0.76, "N": 0.71, "O": 0.66, "F": 0.57, "Na": 1.66,
        "Mg": 1.41, "Si": 1.11, "P": 1.07, "S": 1.05, "Cl": 1.02, "K": 2.03, "Ca": 1.76,
        "Mn": 1.39, "Fe": 1.32, "Zn": 1.22, "Se": 1.20, "Br": 1.20, "I": 1.39,
    }
    tol, default_r = 0.45, 0.77

    heavy = pose["heavy_atoms"]
    if template.GetNumAtoms() != len(heavy):
        return None, None, "heavy-atom count mismatch for VF2 graft."

    els = [a.element for a in heavy]
    pts = np.array([[float(a.x), float(a.y), float(a.z)] for a in heavy], dtype=float)
    pg = nx.Graph()
    for i, el in enumerate(els):
        pg.add_node(i, el=el)
    for i in range(len(heavy)):
        ri = cov.get(els[i], default_r)
        for j in range(i + 1, len(heavy)):
            if float(np.linalg.norm(pts[i] - pts[j])) < ri + cov.get(els[j], default_r) + tol:
                pg.add_edge(i, j)

    tg = nx.Graph()
    for a in template.GetAtoms():
        tg.add_node(a.GetIdx(), el=a.GetSymbol())
    for b in template.GetBonds():
        tg.add_edge(b.GetBeginAtomIdx(), b.GetEndAtomIdx())

    gm = isomorphism.GraphMatcher(
        pg, tg, node_match=isomorphism.categorical_node_match("el", None)
    )
    if not gm.is_isomorphic():
        return None, None, "pose graph not isomorphic to template (VF2)."

    out = Chem.Mol(template)
    conf = Chem.Conformer(out.GetNumAtoms())
    for pose_idx, tmpl_idx in gm.mapping.items():
        conf.SetAtomPosition(tmpl_idx, Point3D(*[float(v) for v in pts[pose_idx]]))
    out.RemoveAllConformers()
    out.AddConformer(conf, assignId=True)
    mol_h = Chem.AddHs(out, addCoords=True)
    return mol_h, desc.CalcMolFormula(mol_h), None


def template_formula(template) -> str:
    from rdkit import Chem  # lazy
    from rdkit.Chem import rdMolDescriptors as desc

    try:
        return desc.CalcMolFormula(Chem.AddHs(template))
    except Exception:  # noqa: BLE001
        return desc.CalcMolFormula(template)


def attempt_atom_mapping(
    pose: Dict[str, Any],
    *,
    chemistry_file: Optional[str],
    smiles: Optional[str],
    chem_source: str,
) -> Dict[str, Any]:
    """Atom-mapping feasibility for a single pose (CONTRACT §7 atom_mapping block)."""
    result: Dict[str, Any] = {
        "attempted": True,
        "success": False,
        "template_heavy_atoms": None,
        "pose_heavy_atoms": len(pose["heavy_atoms"]),
        "molformula_template": None,
        "molformula_pose": None,
        "matched_atoms": 0,
        "message": "",
    }

    template, err = load_template_mol(
        chemistry_file=chemistry_file, smiles=smiles, chem_source=chem_source
    )
    if template is None:
        result["message"] = err or "Failed to load chemistry template."
        return result

    result["template_heavy_atoms"] = template.GetNumAtoms()
    result["molformula_template"] = template_formula(template)

    mapped_h, formula_pose, err = map_pose_to_template(pose, template=template)
    if mapped_h is None:
        result["message"] = err or "Atom mapping failed."
        return result

    result["molformula_pose"] = formula_pose
    result["matched_atoms"] = template.GetNumAtoms()

    formula_template = result["molformula_template"]
    if formula_template and formula_pose and formula_template != formula_pose:
        result["success"] = False
        result["message"] = (
            f"Bond orders assigned but molecular formula differs "
            f"(template {formula_template} vs pose {formula_pose})."
        )
        return result

    result["success"] = True
    result["message"] = "Bond orders assignable from chemistry template to pose."
    return result
