"""Peptide–compound docking with AutoDock Vina.

The *peptide* is treated as the receptor (rigid) and the fixed target *compound* as the
flexible ligand. The compound is prepared once per design job (RDKit 3D embed + Meeko ->
PDBQT) and reused; the receptor PDBQT is built per candidate peptide. Docking is "blind"
(box covers the whole peptide + margin) because a designed peptide has no predefined pocket.

``dock_peptide_compound`` returns a :class:`DockResult` whose ``score`` is the best Vina
affinity in kcal/mol (more negative = stronger predicted binding). All external tools
(``obabel``, ``mk_prepare_ligand.py``, the ``vina`` Python package) are required; a missing
tool raises a clear error so the caller can surface it rather than silently degrade.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .peptide import build_peptide


def _safe_token(sequence: str) -> str:
    """Filesystem-safe, collision-resistant subdir name for a sequence.

    Keeps the (capped) alphanumeric prefix for readability and appends a short hash of the
    full normalized sequence so values with ``/``, ``..`` or reserved characters can neither
    escape the work tree nor collide with a distinct sequence."""
    norm = sequence.strip().upper()
    prefix = re.sub(r"[^A-Z0-9]", "", norm)[:24] or "seq"
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}_{digest}"


@dataclass
class DockResult:
    sequence: str
    score: float                      # best Vina affinity, kcal/mol (negative = better)
    pose_pdbqt: Optional[str] = None  # path to the best-pose PDBQT
    receptor_pdbqt: Optional[str] = None
    peptide_pdb: Optional[str] = None
    center: List[float] = field(default_factory=list)
    box_size: List[float] = field(default_factory=list)


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise RuntimeError(f"Required docking tool not found on PATH: {tool!r}.")
    return path


def prepare_ligand(compound: str, out_pdbqt: Path) -> Path:
    """Prepare the target compound as a Vina ligand PDBQT.

    ``compound`` is a path to a .sdf/.mol/.mol2/.pdb file or a raw SMILES string. RDKit adds
    hydrogens, embeds a 3D conformer (ETKDG) and MMFF-optimizes it; Meeko then writes the
    PDBQT (Gasteiger charges, rotatable-bond detection). Done once per design job.
    """
    from rdkit import Chem
    from rdkit.Chem import AllChem

    out_pdbqt = Path(out_pdbqt)
    out_pdbqt.parent.mkdir(parents=True, exist_ok=True)

    mol = _load_molecule(compound)
    if mol is None:
        raise ValueError(f"Could not parse compound input: {compound!r}")
    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = 0xC0FFEE
    if AllChem.EmbedMolecule(mol, params) != 0:
        # Retry with random coords as a fallback for awkward molecules.
        params.useRandomCoords = True
        if AllChem.EmbedMolecule(mol, params) != 0:
            raise RuntimeError("RDKit failed to embed a 3D conformer for the compound.")
    try:
        AllChem.MMFFOptimizeMolecule(mol)
    except Exception:  # noqa: BLE001 — optimization is best-effort; geometry is already valid
        pass

    sdf3d = out_pdbqt.with_suffix(".prep.sdf")
    w = Chem.SDWriter(str(sdf3d))
    w.write(mol)
    w.close()

    mk = _require("mk_prepare_ligand.py")
    proc = subprocess.run([mk, "-i", str(sdf3d), "-o", str(out_pdbqt)],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out_pdbqt.exists():
        raise RuntimeError(f"mk_prepare_ligand.py failed: {(proc.stderr or proc.stdout)[-400:]}")
    return out_pdbqt


def _load_molecule(compound: str):
    """RDKit Mol from a structure file (sdf/mol/mol2/pdb) or a SMILES string."""
    from rdkit import Chem

    p = Path(compound)
    if p.exists():
        suf = p.suffix.lower()
        if suf in (".sdf", ".mol"):
            return Chem.MolFromMolFile(str(p), removeHs=False)
        if suf == ".mol2":
            return Chem.MolFromMol2File(str(p), removeHs=False)
        if suf == ".pdb":
            return Chem.MolFromPDBFile(str(p), removeHs=False)
        # Unknown extension: try to read its text as SMILES.
        return Chem.MolFromSmiles(p.read_text().strip().splitlines()[0])
    return Chem.MolFromSmiles(compound.strip())


def prepare_receptor(peptide_pdb: Path, out_pdbqt: Path) -> Path:
    """Build a rigid receptor PDBQT from a peptide PDB via Open Babel (add H at pH 7.4)."""
    obabel = _require("obabel")
    out_pdbqt = Path(out_pdbqt)
    out_pdbqt.parent.mkdir(parents=True, exist_ok=True)
    # Remove any stale output first so a failed run can't be mistaken for success.
    if out_pdbqt.exists():
        out_pdbqt.unlink()
    proc = subprocess.run(
        [obabel, str(peptide_pdb), "-O", str(out_pdbqt), "-xr", "-p", "7.4"],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not out_pdbqt.exists() or out_pdbqt.stat().st_size == 0:
        raise RuntimeError(f"obabel receptor prep failed (rc={proc.returncode}): "
                           f"{(proc.stderr or proc.stdout)[-400:]}")
    return out_pdbqt


def _box_from_pdbqt(receptor_pdbqt: Path, margin: float = 8.0):
    """Blind-docking box: center on the receptor bounding box, size = extent + 2*margin."""
    xs, ys, zs = [], [], []
    for line in receptor_pdbqt.read_text(errors="replace").splitlines():
        if line.startswith(("ATOM", "HETATM")) and len(line) >= 54:
            try:
                xs.append(float(line[30:38])); ys.append(float(line[38:46])); zs.append(float(line[46:54]))
            except ValueError:
                continue
    if not xs:
        raise RuntimeError("Receptor PDBQT has no parseable atom coordinates for box sizing.")
    center = [(min(c) + max(c)) / 2.0 for c in (xs, ys, zs)]
    size = [max(6.0, (max(c) - min(c)) + 2.0 * margin) for c in (xs, ys, zs)]
    return center, size


def dock_peptide_compound(
    sequence: str,
    ligand_pdbqt: Path,
    workdir: Path,
    *,
    geometry: str = "extended",
    exhaustiveness: int = 8,
    n_poses: int = 5,
    margin: float = 8.0,
    cpu: int = 4,
    seed: int = 0,
) -> DockResult:
    """Build ``sequence``, dock the (pre-prepared) compound against it, return the best score.

    All per-candidate files are written under ``workdir/<sequence>/`` so that concurrent
    docking of distinct sequences sharing a ``workdir`` cannot clobber each other's inputs or
    poses (the GA additionally dedupes identical sequences before dispatch).
    """
    from vina import Vina

    workdir = Path(workdir) / _safe_token(sequence)
    workdir.mkdir(parents=True, exist_ok=True)
    peptide_pdb = build_peptide(sequence, workdir / "peptide.pdb", geometry=geometry)
    receptor_pdbqt = prepare_receptor(peptide_pdb, workdir / "receptor.pdbqt")
    center, box = _box_from_pdbqt(receptor_pdbqt, margin=margin)

    v = Vina(sf_name="vina", cpu=max(1, cpu), seed=seed, verbosity=0)
    v.set_receptor(str(receptor_pdbqt))
    v.set_ligand_from_file(str(ligand_pdbqt))
    v.compute_vina_maps(center=center, box_size=box)
    v.dock(exhaustiveness=exhaustiveness, n_poses=n_poses)

    pose_path = workdir / "docked_poses.pdbqt"
    v.write_poses(str(pose_path), n_poses=min(n_poses, 5), overwrite=True)
    energies = v.energies(n_poses=n_poses)
    best = float(energies[0][0]) if len(energies) else float("inf")

    return DockResult(
        sequence=sequence, score=round(best, 3),
        pose_pdbqt=str(pose_path), receptor_pdbqt=str(receptor_pdbqt),
        peptide_pdb=str(peptide_pdb), center=[round(c, 3) for c in center],
        box_size=[round(s, 3) for s in box],
    )
