"""AutoDock Vina PDBQT parsing (import-light, no RDKit).

Parses multi-MODEL PDBQT files into pose dictionaries with per-atom element symbols
(resolved from AutoDock atom types), heavy-atom subsets, and 'REMARK VINA RESULT' scores.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


# AutoDock atom types -> element symbol. PDBQT column ~78 carries the AD atom type
# (e.g. "A" aromatic carbon, "C", "OA"/"OS" acceptor oxygen, "NA"/"N", "HD" polar H, ...).
AD_TYPE_TO_ELEMENT: Dict[str, str] = {
    "C": "C", "A": "C",
    "N": "N", "NA": "N", "NS": "N",
    "O": "O", "OA": "O", "OS": "O",
    "S": "S", "SA": "S",
    "H": "H", "HD": "H", "HS": "H",
    "P": "P",
    "F": "F", "CL": "Cl", "BR": "Br", "I": "I",
    "MG": "Mg", "MN": "Mn", "ZN": "Zn", "CA": "Ca", "FE": "Fe",
    "K": "K",
    "B": "B", "SI": "Si", "SE": "Se",
}

HYDROGEN_ELEMENTS = {"H"}

_TWO_LETTER = {"Cl", "Br", "Si", "Se", "Mg", "Mn", "Zn", "Ca", "Fe", "Na"}
_ONE_LETTER = {"C", "N", "O", "S", "P", "H", "F", "I", "B", "K"}


def ad_type_to_element(ad_type: str, fallback_name: str = "") -> str:
    """Map an AutoDock atom-type token to an element symbol.

    Falls back to parsing the leading alpha characters of the atom name when the type is
    unknown (covers exotic PDBQTs).
    """
    t = (ad_type or "").strip()
    if not t:
        t = (fallback_name or "").strip()
    key = t.upper()
    if key in AD_TYPE_TO_ELEMENT:
        return AD_TYPE_TO_ELEMENT[key]
    alpha = re.sub(r"[^A-Za-z]", "", t)
    if not alpha:
        return "C"
    two = alpha[:2].capitalize()
    one = alpha[:1].upper()
    if two in _TWO_LETTER:
        return two
    if one in _ONE_LETTER:
        return one
    return one


class PdbqtAtom:
    __slots__ = ("serial", "name", "element", "x", "y", "z", "is_hydrogen", "ad_type")

    def __init__(self, serial, name, element, x, y, z, ad_type):
        self.serial = serial
        self.name = name
        self.element = element
        self.x = x
        self.y = y
        self.z = z
        self.ad_type = ad_type
        self.is_hydrogen = element in HYDROGEN_ELEMENTS


def parse_pdbqt_atom_line(line: str) -> Optional[PdbqtAtom]:
    if not line.startswith(("ATOM", "HETATM")):
        return None
    try:
        x = float(line[30:38])
        y = float(line[38:46])
        z = float(line[46:54])
    except (ValueError, IndexError):
        return None
    name = line[12:16].strip()
    ad_type = line[77:].strip().split()[0] if len(line) > 77 else ""
    if not ad_type:
        toks = line.rstrip().split()
        ad_type = toks[-1] if toks else ""
    element = ad_type_to_element(ad_type, name)
    try:
        serial = int(line[6:11])
    except (ValueError, IndexError):
        serial = 0
    return PdbqtAtom(serial, name, element, x, y, z, ad_type)


def parse_pdbqt_models(path: str | os.PathLike) -> List[Dict[str, Any]]:
    """Parse a multi-MODEL PDBQT into a list of pose dicts.

    Each pose dict has keys: index, docking_score, atoms (list[PdbqtAtom]),
    heavy_atoms (list[PdbqtAtom]), raw_lines (list[str]). Scores come from the
    'REMARK VINA RESULT: <affinity> <rmsd_lb> <rmsd_ub>' line (first number = affinity).
    A file with no MODEL records is treated as a single pose.
    """
    text = Path(path).read_text(errors="replace")
    lines = text.splitlines()

    poses: List[Dict[str, Any]] = []
    cur_atoms: List[PdbqtAtom] = []
    cur_lines: List[str] = []
    cur_score: Optional[float] = None
    cur_index = 0
    in_model = False
    any_model = any(ln.startswith("MODEL") for ln in lines)
    fallback_idx = 1

    def _flush(idx_hint: int) -> None:
        nonlocal cur_atoms, cur_lines, cur_score, cur_index
        if not cur_atoms:
            return
        heavy = [a for a in cur_atoms if not a.is_hydrogen]
        poses.append(
            {
                "index": cur_index if cur_index else idx_hint,
                "docking_score": cur_score,
                "atoms": cur_atoms,
                "heavy_atoms": heavy,
                "raw_lines": cur_lines,
            }
        )
        cur_atoms = []
        cur_lines = []
        cur_score = None
        cur_index = 0

    for ln in lines:
        if ln.startswith("MODEL"):
            in_model = True
            cur_atoms = []
            cur_lines = []
            cur_score = None
            m = re.match(r"MODEL\s+(\d+)", ln)
            cur_index = int(m.group(1)) if m else 0
            continue
        if ln.startswith("ENDMDL"):
            _flush(fallback_idx)
            fallback_idx += 1
            in_model = False
            continue
        if "VINA RESULT" in ln:
            nums = re.findall(r"[-+]?\d*\.\d+|[-+]?\d+", ln.split("VINA RESULT")[-1])
            if nums:
                try:
                    cur_score = float(nums[0])
                except ValueError:
                    cur_score = None
            continue
        if ln.startswith(("ATOM", "HETATM")):
            atom = parse_pdbqt_atom_line(ln)
            if atom is not None:
                cur_atoms.append(atom)
                cur_lines.append(ln)

    if not any_model and cur_atoms:
        _flush(1)
    elif in_model and cur_atoms:
        _flush(fallback_idx)

    for i, p in enumerate(poses, start=1):
        if not p["index"]:
            p["index"] = i
    return poses


def heavy_element_counts(pose: Dict[str, Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for a in pose["heavy_atoms"]:
        counts[a.element] = counts.get(a.element, 0) + 1
    return counts


def hill_formula(element_counts: Dict[str, int]) -> str:
    """Build a Hill-system formula string from an element->count dict (heavy atoms only)."""
    if not element_counts:
        return ""
    counts = dict(element_counts)
    parts: List[str] = []
    if "C" in counts:
        c = counts.pop("C")
        parts.append("C" + (str(c) if c != 1 else ""))
        if "H" in counts:
            h = counts.pop("H")
            parts.append("H" + (str(h) if h != 1 else ""))
    for el in sorted(counts):
        n = counts[el]
        parts.append(el + (str(n) if n != 1 else ""))
    return "".join(parts)
