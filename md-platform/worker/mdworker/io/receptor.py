"""Receptor PDB / mmCIF metadata parsing (import-light, no RDKit).

Scope: extract chains, residue count, atom count, and HETATM residue tallies for the
ValidationReport. The mmCIF reader is a *basic* loop reader: it handles standard
whitespace-delimited `_atom_site` loops (as produced by the PDB and common tools) and
single-token quoted values, but does not attempt full CIF grammar (multi-line text
fields, nested save frames). Files that exceed that scope are reported with a warning
rather than misread; for those, supplying a PDB receptor is recommended.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Tuple


STANDARD_AA = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
    "SEC", "PYL", "HID", "HIE", "HIP", "CYX", "ASH", "GLH", "LYN",
}

WATER_NAMES = {"HOH", "WAT", "H2O", "SOL", "TIP", "TIP3"}
ION_NAMES = {"NA", "CL", "K", "MG", "ZN", "CA", "FE", "MN", "SO4", "PO4", "CLA", "SOD", "POT"}
COFACTOR_NAMES = {"HEM", "FAD", "FMN", "NAD", "NAP", "ATP", "ADP", "GTP", "GDP", "NADP", "PLP", "SAM"}


def suggest_hetatm(resname: str) -> str:
    """Classify a HETATM residue name into a suggested handling category (CONTRACT §7)."""
    rn = (resname or "").upper()
    if rn in WATER_NAMES:
        return "water"
    if rn in ION_NAMES:
        return "ion"
    if rn in COFACTOR_NAMES:
        return "cofactor"
    return "ligand"


def _split_cif_tokens(line: str) -> List[str]:
    """Split a CIF data line honoring single/double quoted single-token values."""
    tokens: List[str] = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c.isspace():
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            i += 1
            start = i
            while i < n and line[i] != quote:
                i += 1
            tokens.append(line[start:i])
            i += 1  # skip closing quote
        else:
            start = i
            while i < n and not line[i].isspace():
                i += 1
            tokens.append(line[start:i])
    return tokens


def cif_to_pdb_lines(
    cif_path: str | os.PathLike, *, hetatm_decisions: Dict[str, str] | None = None
) -> Tuple[List[str], List[str]]:
    """Convert a basic mmCIF `_atom_site` loop to PDB ATOM/HETATM lines.

    Preserves chain, residue number, insertion code, altLoc, element, and only the first
    model (pdbx_PDB_model_num == first seen) so multi-model CIFs do not duplicate atoms.
    Honors HETATM drop/water decisions (resname -> drop|water removes the record). Scope
    matches parse_receptor: standard whitespace/quoted loop values, no multi-line text
    fields. Returns (pdb_lines, warnings). Centralizing this here keeps a single validated
    receptor parser (no duplicate CIF logic in pipeline steps).
    """
    decisions = hetatm_decisions or {}
    lines = Path(cif_path).read_text(errors="replace").splitlines()
    col_order: List[str] = []
    reading_cols = False
    reading_rows = False
    warnings: List[str] = []
    out: List[str] = []
    serial = 0
    first_model: str | None = None

    for ln in lines:
        s = ln.strip()
        if s.startswith("_atom_site."):
            col_order.append(s.split(".", 1)[1])
            reading_cols = True
            reading_rows = False
            continue
        if reading_cols and not s.startswith("_atom_site."):
            reading_cols = False
            reading_rows = bool(col_order)
        if reading_rows:
            if not s or s.startswith("#") or s.startswith("_") or s.lower().startswith("loop_"):
                break
            if s[:1] == ";":
                warnings.append("mmCIF multi-line text field encountered; conversion stopped early.")
                break
            toks = _split_cif_tokens(s)
            if len(toks) < len(col_order):
                continue
            rec = dict(zip(col_order, toks))

            model = rec.get("pdbx_PDB_model_num")
            if model is not None:
                if first_model is None:
                    first_model = model
                elif model != first_model:
                    continue  # skip additional NMR/ensemble models

            group = rec.get("group_PDB", "ATOM")
            resname = (rec.get("auth_comp_id") or rec.get("label_comp_id") or "UNK").upper()
            if group == "HETATM":
                decision = decisions.get(resname) or ("water" if resname in WATER_NAMES else "keep")
                if decision in ("drop", "water"):
                    continue
            try:
                x = float(rec.get("Cartn_x", "0"))
                y = float(rec.get("Cartn_y", "0"))
                z = float(rec.get("Cartn_z", "0"))
            except ValueError:
                continue

            serial += 1
            atom_name = (rec.get("auth_atom_id") or rec.get("label_atom_id") or "C").strip()
            chain = (rec.get("auth_asym_id") or rec.get("label_asym_id") or "A")[:1]
            resseq = rec.get("auth_seq_id") or rec.get("label_seq_id") or str(serial)
            icode = rec.get("pdbx_PDB_ins_code") or ""
            if icode in (".", "?"):
                icode = ""
            altloc = rec.get("label_alt_id") or ""
            if altloc in (".", "?"):
                altloc = ""
            element = (rec.get("type_symbol") or "".join(c for c in atom_name if c.isalpha())[:1] or "C").strip()
            try:
                resseq_i = int(resseq)
            except ValueError:
                resseq_i = serial
            # Atom-name column alignment: 1-char element names indent one space.
            if len(element) == 1 and len(atom_name) < 4:
                name_field = (" " + atom_name).ljust(4)
            else:
                name_field = atom_name[:4].ljust(4)
            out.append(
                f"{group:<6}{serial % 100000:>5} {name_field}{altloc[:1] or ' '}{resname[:3]:>3} "
                f"{chain}{resseq_i % 10000:>4}{icode[:1] or ' '}   "
                f"{x:>8.3f}{y:>8.3f}{z:>8.3f}{1.0:>6.2f}{0.0:>6.2f}          {element:>2}"
            )

    if serial == 0:
        warnings.append("No mmCIF _atom_site records converted; receptor format unsupported.")
        return [], warnings
    out.append("TER")
    out.append("END")
    return out, warnings


def parse_receptor(path: str | os.PathLike) -> Tuple[Dict[str, Any], List[str]]:
    """Parse a receptor PDB/CIF file.

    Returns (info_dict, warnings). info_dict has keys: format, chains, n_residues,
    n_atoms, has_hetatm, hetatm_resnames.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    fmt = "cif" if suffix in (".cif", ".mmcif") else "pdb"
    warnings: List[str] = []

    chains: set = set()
    residues: set = set()
    n_atoms = 0
    has_hetatm = False
    # Count unique HETATM residues (not atoms) per resname so multi-atom ligands/cofactors
    # are tallied once. Keyed by (chain, resseq, icode, resname) seen-set.
    hetatm_res_keys: set = set()
    hetatm_resnames: Dict[str, int] = {}

    def _bump_hetatm(resname: str, key: Tuple) -> None:
        if key in hetatm_res_keys:
            return
        hetatm_res_keys.add(key)
        hetatm_resnames[resname] = hetatm_resnames.get(resname, 0) + 1

    text = p.read_text(errors="replace")

    if fmt == "pdb":
        for ln in text.splitlines():
            if ln.startswith("ATOM"):
                n_atoms += 1
                chain = ln[21:22].strip() or "A"
                resseq = ln[22:26].strip()
                icode = ln[26:27].strip()
                resname = ln[17:20].strip()
                chains.add(chain)
                residues.add((chain, resseq, icode, resname))
            elif ln.startswith("HETATM"):
                n_atoms += 1
                has_hetatm = True
                resname = ln[17:20].strip()
                resseq = ln[22:26].strip()
                icode = ln[26:27].strip()
                chain = ln[21:22].strip() or "A"
                chains.add(chain)
                _bump_hetatm(resname, (chain, resseq, icode, resname))
    else:
        lines = text.splitlines()
        col_order: List[str] = []
        reading_cols = False
        reading_rows = False
        for ln in lines:
            s = ln.strip()
            if s.startswith("_atom_site."):
                col_order.append(s.split(".", 1)[1])
                reading_cols = True
                reading_rows = False
                continue
            if reading_cols and not s.startswith("_atom_site."):
                # Column header block ended; data rows (or another section) follow.
                reading_cols = False
                reading_rows = bool(col_order)
            if reading_rows:
                if not s or s.startswith("#") or s.startswith("_") or s.lower().startswith("loop_"):
                    if col_order:
                        break
                    continue
                if ";" in s[:1]:
                    warnings.append("mmCIF multi-line text field encountered; receptor metadata may be partial.")
                    break
                toks = _split_cif_tokens(s)
                if len(toks) < len(col_order):
                    continue
                rec = dict(zip(col_order, toks))
                group = rec.get("group_PDB", "ATOM")
                n_atoms += 1
                chain = rec.get("auth_asym_id") or rec.get("label_asym_id") or "A"
                resname = rec.get("auth_comp_id") or rec.get("label_comp_id") or ""
                resseq = rec.get("auth_seq_id") or rec.get("label_seq_id") or ""
                icode = rec.get("pdbx_PDB_ins_code") or ""
                if icode in (".", "?"):
                    icode = ""
                chains.add(chain)
                if group == "HETATM":
                    has_hetatm = True
                    _bump_hetatm(resname, (chain, resseq, icode, resname))
                else:
                    residues.add((chain, resseq, icode, resname))
        if fmt == "cif" and n_atoms == 0:
            warnings.append("No mmCIF _atom_site records parsed; receptor metadata unavailable.")

    info = {
        "format": fmt,
        "chains": sorted(c for c in chains if c),
        "n_residues": len(residues),
        "n_atoms": n_atoms,
        "has_hetatm": has_hetatm,
        "hetatm_resnames": hetatm_resnames,
    }
    return info, warnings
