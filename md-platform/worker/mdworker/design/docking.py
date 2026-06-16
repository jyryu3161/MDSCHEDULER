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
    score: float                      # best affinity, kcal/mol (negative = better)
    pose_pdbqt: Optional[str] = None  # path to the best-pose PDBQT
    receptor_pdbqt: Optional[str] = None
    peptide_pdb: Optional[str] = None
    center: List[float] = field(default_factory=list)
    box_size: List[float] = field(default_factory=list)
    engine: str = "vina"              # which docking engine produced this score
    all_scores: List[float] = field(default_factory=list)  # per-pose affinities (best first)
    top2_gap: Optional[float] = None  # affinity gap between pose 1 and 2 (<~1 ⇒ ambiguous)
    n_flexres: Optional[int] = None   # flexible receptor side chains used (smina), else None


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
    # EmbedMolecule returns a conformer id (>= 0) on success and -1 on failure, so the failure
    # test is "< 0" (not "!= 0", which would wrongly treat a non-zero conformer id as failure).
    if AllChem.EmbedMolecule(mol, params) < 0:
        # Retry with random coords as a fallback for awkward molecules.
        params.useRandomCoords = True
        if AllChem.EmbedMolecule(mol, params) < 0:
            raise RuntimeError("RDKit failed to embed a 3D conformer for the compound.")
    try:
        AllChem.MMFFOptimizeMolecule(mol)
    except Exception:  # noqa: BLE001 — optimization is best-effort; geometry is already valid
        pass

    sdf3d = out_pdbqt.with_suffix(".prep.sdf")
    w = Chem.SDWriter(str(sdf3d))
    w.write(mol)
    w.close()
    # Validate the intermediate SDF before handing it to Meeko, so an incomplete RDKit write
    # surfaces here rather than as a cryptic meeko failure (mirrors prepare_receptor's guard).
    if not sdf3d.exists() or sdf3d.stat().st_size == 0:
        raise RuntimeError(f"RDKit SDF output failed or empty: {sdf3d}")

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


def _box_from_pdbqt(receptor_pdbqt: Path, margin: float = 8.0, *, seq_len: int = 0):
    """Blind-docking box: center on the receptor bounding box, size = extent + 2*margin.

    A designed peptide has no predefined pocket, so the box must enclose the whole peptide.
    The margin scales gently with peptide length (longer chains need wider coverage) and is
    capped so short peptides aren't over-boxed; the 6 Å floor keeps tiny systems sane.
    """
    eff_margin = min(12.0, margin + 0.5 * max(0, seq_len))
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
    size = [max(6.0, (max(c) - min(c)) + 2.0 * eff_margin) for c in (xs, ys, zs)]
    return center, size


_ENGINES = ("auto", "smina", "vina")


def resolve_engine(engine: str) -> str:
    """Resolve the docking engine. 'auto' -> smina if on PATH else vina; 'smina'/'vina' pass
    through. Any other value raises (an unknown engine must never silently fall back, which
    would mislabel DockResult.engine and hide a config typo)."""
    engine = (engine or "auto").strip().lower()
    if engine not in _ENGINES:
        raise ValueError(f"Unknown docking engine {engine!r}; expected one of {_ENGINES}.")
    if engine == "auto":
        return "smina" if shutil.which("smina") else "vina"
    return engine


def _dock_vina(receptor_pdbqt: Path, ligand_pdbqt: Path, center, box, pose_path: Path,
               *, exhaustiveness: int, n_poses: int, cpu: int, seed: int):
    """Rigid-receptor AutoDock Vina (Python API). Returns (best, all_scores, pose_path)."""
    from vina import Vina

    v = Vina(sf_name="vina", cpu=max(1, cpu), seed=seed, verbosity=0)
    v.set_receptor(str(receptor_pdbqt))
    v.set_ligand_from_file(str(ligand_pdbqt))
    v.compute_vina_maps(center=list(center), box_size=list(box))
    v.dock(exhaustiveness=exhaustiveness, n_poses=n_poses)
    v.write_poses(str(pose_path), n_poses=min(n_poses, 10), overwrite=True)
    energies = v.energies(n_poses=n_poses)
    all_scores = [round(float(e[0]), 3) for e in energies] if len(energies) else []
    # Raise rather than return inf when Vina finds no poses, matching _dock_smina: a docking
    # failure must surface (caller maps it to a failed candidate), never masquerade as a score.
    if not all_scores:
        raise RuntimeError("Vina produced no poses with energies.")
    best = all_scores[0]
    return best, all_scores, 0


def _parse_smina_scores(pose_pdbqt: Path) -> List[float]:
    """Best-first per-pose affinities from a smina output PDBQT (REMARK minimizedAffinity).

    An affinity-prefixed REMARK that fails to parse is treated as corruption and RAISES (each
    pose carries exactly one such line, so a malformed one means a partial/garbled output that
    must not silently shorten the score list). Non-affinity REMARK lines are ignored.
    """
    scores: List[float] = []
    for line in pose_pdbqt.read_text(errors="replace").splitlines():
        # smina writes either "REMARK minimizedAffinity <v>" or "REMARK VINA RESULT: <v> ..."
        if line.startswith("REMARK minimizedAffinity"):
            try:
                scores.append(round(float(line.split()[-1]), 3))
            except (ValueError, IndexError):
                raise RuntimeError(f"Malformed smina affinity line: {line!r}")
        elif line.startswith("REMARK VINA RESULT"):
            try:
                scores.append(round(float(line.split(":")[1].split()[0]), 3))
            except (ValueError, IndexError):
                raise RuntimeError(f"Malformed smina VINA RESULT line: {line!r}")
    return scores


def _dock_smina(receptor_pdbqt: Path, ligand_pdbqt: Path, center, box, pose_path: Path,
                *, exhaustiveness: int, n_poses: int, cpu: int, seed: int,
                flexdist: float, scoring: str = "vina"):
    """Smina with auto-selected flexible receptor side chains (--flexdist around the ligand).

    Smina shares Vina's scoring function but adds receptor side-chain flexibility and fast
    local minimization — the two things a designed-peptide receptor needs. Returns
    (best, all_scores, n_flexres). Parses affinities from the output PDBQT; a parse failure
    raises (rather than silently returning inf) so a bug can't masquerade as a bad candidate.
    """
    smina = _require("smina")
    if pose_path.exists():
        pose_path.unlink()
    cmd = [
        smina, "-r", str(receptor_pdbqt), "-l", str(ligand_pdbqt),
        "--center_x", f"{center[0]:.3f}", "--center_y", f"{center[1]:.3f}", "--center_z", f"{center[2]:.3f}",
        "--size_x", f"{box[0]:.3f}", "--size_y", f"{box[1]:.3f}", "--size_z", f"{box[2]:.3f}",
        "--exhaustiveness", str(exhaustiveness), "--num_modes", str(n_poses),
        "--cpu", str(max(1, cpu)), "--seed", str(seed), "--scoring", scoring,
        "--flexdist_ligand", str(ligand_pdbqt), "--flexdist", f"{flexdist:.2f}",
        "--out", str(pose_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0 or not pose_path.exists() or pose_path.stat().st_size == 0:
        raise RuntimeError(f"smina failed (rc={proc.returncode}): {(proc.stderr or proc.stdout)[-400:]}")
    all_scores = _parse_smina_scores(pose_path)
    if not all_scores:
        raise RuntimeError(f"smina produced no parseable affinity in {pose_path.name}.")
    # Count flexible side chains from the flex output if smina wrote one (best-effort).
    n_flex = None
    flex_out = pose_path.with_name(pose_path.stem + "_flex.pdbqt")
    if flex_out.exists():
        n_flex = sum(1 for ln in flex_out.read_text(errors="replace").splitlines() if ln.startswith("BEGIN_RES"))
    return all_scores[0], all_scores, n_flex


def dock_peptide_compound(
    sequence: str,
    ligand_pdbqt: Path,
    workdir: Path,
    *,
    engine: str = "vina",
    geometry: str = "extended",
    exhaustiveness: int = 16,
    n_poses: int = 5,
    margin: float = 8.0,
    flexdist: float = 3.5,
    cpu: int = 4,
    seed: int = 0,
) -> DockResult:
    """Build ``sequence``, dock the (pre-prepared) compound against it, return the best score.

    ``engine``: "vina" (DEFAULT — AutoDock Vina 1.2.7, rigid receptor, deterministic), "smina"
    (opt-in — adds flexible receptor side chains via --flexdist), or "auto" (smina if on PATH,
    else vina). Docking is a coarse high-recall pre-screen for the GA; MD + MM/GBSA is the real
    ranking arbiter. All per-candidate files live under ``workdir/<token(sequence)>/`` so
    concurrent docking of distinct sequences can't collide.
    """
    eng = resolve_engine(engine)
    workdir = Path(workdir) / _safe_token(sequence)
    workdir.mkdir(parents=True, exist_ok=True)
    peptide_pdb = build_peptide(sequence, workdir / "peptide.pdb", geometry=geometry)
    receptor_pdbqt = prepare_receptor(peptide_pdb, workdir / "receptor.pdbqt")
    center, box = _box_from_pdbqt(receptor_pdbqt, margin=margin, seq_len=len(sequence.strip()))
    pose_path = workdir / "docked_poses.pdbqt"

    if eng == "smina":
        best, all_scores, n_flex = _dock_smina(
            receptor_pdbqt, ligand_pdbqt, center, box, pose_path,
            exhaustiveness=exhaustiveness, n_poses=n_poses, cpu=cpu, seed=seed, flexdist=flexdist)
    else:
        best, all_scores, n_flex = _dock_vina(
            receptor_pdbqt, ligand_pdbqt, center, box, pose_path,
            exhaustiveness=exhaustiveness, n_poses=n_poses, cpu=cpu, seed=seed)
        n_flex = None

    top2_gap = round(all_scores[1] - all_scores[0], 3) if len(all_scores) >= 2 else None
    return DockResult(
        sequence=sequence, score=round(float(best), 3),
        pose_pdbqt=str(pose_path), receptor_pdbqt=str(receptor_pdbqt),
        peptide_pdb=str(peptide_pdb), center=[round(c, 3) for c in center],
        box_size=[round(s, 3) for s in box], engine=eng, all_scores=all_scores,
        top2_gap=top2_gap, n_flexres=n_flex,
    )
