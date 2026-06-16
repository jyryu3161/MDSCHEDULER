"""Shared structure utilities (import-light + numpy): read/write PDB atoms, assemble the
receptor+ligand complex, and produce a multi-MODEL trajectory PDB.

Used by both the mock engine (synthetic trajectory) and analyze_md (reading frames). Kept
free of RDKit; ligand atoms come from the assign_bond_orders output PDB.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np


@dataclass
class Atom:
    record: str  # "ATOM" or "HETATM"
    serial: int
    name: str
    resname: str
    chain: str
    resseq: int
    x: float
    y: float
    z: float
    element: str
    is_backbone: bool = False
    is_ligand: bool = False


_BACKBONE_NAMES = {"N", "CA", "C", "O"}


def parse_pdb_atoms(path: str | Path, *, is_ligand: bool = False) -> List[Atom]:
    """Parse ATOM/HETATM records from a PDB file into Atom objects."""
    atoms: List[Atom] = []
    for ln in Path(path).read_text(errors="replace").splitlines():
        if not ln.startswith(("ATOM", "HETATM")):
            continue
        try:
            x = float(ln[30:38])
            y = float(ln[38:46])
            z = float(ln[46:54])
        except (ValueError, IndexError):
            continue
        record = ln[0:6].strip()
        name = ln[12:16].strip()
        resname = ln[17:20].strip() or ("LIG" if is_ligand else "UNK")
        chain = ln[21:22].strip() or "A"
        try:
            resseq = int(ln[22:26])
        except (ValueError, IndexError):
            resseq = 1
        try:
            serial = int(ln[6:11])
        except (ValueError, IndexError):
            serial = len(atoms) + 1
        element = ln[76:78].strip()
        if not element:
            element = "".join(c for c in name if c.isalpha())[:1] or "C"
        atoms.append(
            Atom(
                record="HETATM" if is_ligand else record,
                serial=serial,
                name=name,
                resname=resname,
                chain=chain,
                resseq=resseq,
                x=x,
                y=y,
                z=z,
                element=element,
                is_backbone=(not is_ligand and name in _BACKBONE_NAMES),
                is_ligand=is_ligand,
            )
        )
    return atoms


def coords_array(atoms: Sequence[Atom]) -> np.ndarray:
    return np.array([[a.x, a.y, a.z] for a in atoms], dtype=float)


def _fmt_atom_line(serial: int, a: Atom, xyz: Sequence[float]) -> str:
    """Format a single PDB ATOM/HETATM line (columns per the PDB spec)."""
    name = a.name
    # PDB atom-name column rules: 1-char element names start at col 14 (index 13).
    if len(name) >= 4:
        name_field = name[:4]
    elif len(a.element) == 1 and len(name) <= 3:
        name_field = " " + name.ljust(3)
    else:
        name_field = name.ljust(4)
    resname = (a.resname or "UNK")[:3]
    chain = (a.chain or "A")[:1]
    return (
        f"{a.record:<6}{serial % 100000:>5} {name_field:<4}{'':1}{resname:>3} "
        f"{chain}{a.resseq % 10000:>4}{'':1}   "
        f"{xyz[0]:>8.3f}{xyz[1]:>8.3f}{xyz[2]:>8.3f}"
        f"{1.0:>6.2f}{0.0:>6.2f}          {a.element:>2}"
    )


def write_pdb(atoms: Sequence[Atom], coords: np.ndarray, path: str | Path, *, title: str = "") -> None:
    """Write a single-model PDB."""
    lines: List[str] = []
    if title:
        lines.append(f"TITLE     {title}")
    for i, a in enumerate(atoms):
        lines.append(_fmt_atom_line(i + 1, a, coords[i]))
    lines.append("END")
    Path(path).write_text("\n".join(lines) + "\n")


def write_multimodel_pdb(
    atoms: Sequence[Atom],
    frames: Sequence[np.ndarray],
    path: str | Path,
    *,
    title: str = "",
) -> int:
    """Write a multi-MODEL trajectory PDB (NGL/Mol* loadable). Returns frame count."""
    lines: List[str] = []
    if title:
        lines.append(f"TITLE     {title}")
    for fi, coords in enumerate(frames, start=1):
        lines.append(f"MODEL     {fi:>4}")
        for i, a in enumerate(atoms):
            lines.append(_fmt_atom_line(i + 1, a, coords[i]))
        lines.append("ENDMDL")
    lines.append("END")
    Path(path).write_text("\n".join(lines) + "\n")
    return len(frames)


def assemble_complex(
    receptor_atoms: Sequence[Atom], ligand_atoms: Sequence[Atom]
) -> Tuple[List[Atom], np.ndarray]:
    """Concatenate receptor + ligand atoms into one system.

    Ligand atoms are placed in a dedicated residue (resname MOL) one past the last receptor
    residue, matching the assemble_complex recipe in preprocess_pipeline.sh.
    """
    combined: List[Atom] = list(receptor_atoms)
    last_resseq = max((a.resseq for a in receptor_atoms), default=0)
    lig_resseq = last_resseq + 1
    lig_chain = receptor_atoms[0].chain if receptor_atoms else "A"
    for a in ligand_atoms:
        combined.append(
            Atom(
                record="HETATM",
                serial=a.serial,
                name=a.name,
                resname="MOL",
                chain=lig_chain,
                resseq=lig_resseq,
                x=a.x,
                y=a.y,
                z=a.z,
                element=a.element,
                is_backbone=False,
                is_ligand=True,
            )
        )
    coords = coords_array(combined)
    return combined, coords


def read_multimodel_pdb(path: str | Path) -> Tuple[List[Atom], List[np.ndarray]]:
    """Read a multi-MODEL PDB back into (atoms_from_first_model, [coords per frame])."""
    text = Path(path).read_text(errors="replace")
    frames: List[np.ndarray] = []
    atoms: List[Atom] = []
    cur: List[Tuple[float, float, float]] = []
    in_model = False
    any_model = "MODEL" in text
    first = True

    def _flush():
        nonlocal cur, first
        if cur:
            frames.append(np.array(cur, dtype=float))
        cur = []
        first = False

    for ln in text.splitlines():
        if ln.startswith("MODEL"):
            in_model = True
            cur = []
            continue
        if ln.startswith("ENDMDL"):
            _flush()
            in_model = False
            continue
        if ln.startswith(("ATOM", "HETATM")):
            try:
                x = float(ln[30:38]); y = float(ln[38:46]); z = float(ln[46:54])
            except (ValueError, IndexError):
                continue
            cur.append((x, y, z))
            if first:
                name = ln[12:16].strip()
                element = ln[76:78].strip() or "".join(c for c in name if c.isalpha())[:1] or "C"
                resname = ln[17:20].strip()
                atoms.append(
                    Atom(
                        record=ln[0:6].strip(),
                        serial=len(atoms) + 1,
                        name=name,
                        resname=resname,
                        chain=ln[21:22].strip() or "A",
                        resseq=int(ln[22:26]) if ln[22:26].strip().lstrip("-").isdigit() else 1,
                        x=x, y=y, z=z, element=element,
                        is_backbone=(resname not in ("MOL", "LIG", "UNK") and name in _BACKBONE_NAMES),
                        is_ligand=(resname in ("MOL", "LIG")),
                    )
                )
    if not any_model and cur:
        frames.append(np.array(cur, dtype=float))
    elif in_model and cur:
        frames.append(np.array(cur, dtype=float))
    return atoms, frames
