"""Split a protein+ligand COMPLEX structure (mmCIF or PDB) into the two inputs the existing MD
pipeline already consumes: a protein-only receptor PDB and a single ligand "pose" PDBQT.

Motivation: co-folded complexes (AlphaFold3 / Boltz / Chai, or experimental) come as ONE file
with the protein and the ligand already in their bound geometry — there is no docking. Rather
than add a parallel pipeline, we extract the protein (-> receptor) and the ligand's heavy-atom
coordinates (-> a 1-model pose, no docking score), so the unchanged downstream steps
(assign_bond_orders with the supplied SMILES/SDF -> parameterize -> assemble -> MD) just run.

Ligand identification: the non-polymer residue that is NOT water and NOT a monatomic ion, with
the most heavy atoms (handles a stray ion/water alongside the ligand). gemmi reads mmCIF and PDB
uniformly, so this works for either format.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

# Standard amino acids (incl. common protonation/terminal variants) — used to tell protein
# residues from the ligand without depending on a particular gemmi tabulation version.
_STANDARD_AA = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE", "LEU", "LYS", "MET",
    "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL", "SEC", "PYL", "MSE",
    "HID", "HIE", "HIP", "CYX", "CYM", "ASH", "GLH", "LYN", "ACE", "NME", "NMA",
}
_WATER = {"HOH", "WAT", "DOD", "H2O", "SOL", "TIP", "TIP3", "T3P"}


class ComplexSplitError(ValueError):
    """Raised when a complex cannot be split (no protein, or no ligand residue found)."""


def split_complex(
    structure_path: str | os.PathLike,
    receptor_pdb_out: str | os.PathLike,
    pose_pdbqt_out: str | os.PathLike,
    *,
    ligand_resname: Optional[str] = None,
) -> Dict[str, Any]:
    """Write the protein-only receptor PDB and the ligand heavy-atom pose PDBQT.

    Returns metadata: {ligand_resname, n_ligand_heavy, n_protein_atoms, n_protein_residues}.
    Raises ComplexSplitError if no protein or no ligand can be identified.
    """
    import gemmi

    st = gemmi.read_structure(str(structure_path))
    try:
        st.setup_entities()
    except Exception:  # noqa: BLE001 — entity setup is best-effort; classification below stands alone
        pass
    if not len(st):
        raise ComplexSplitError("Structure has no models.")
    model = st[0]

    # ---- locate the ligand residue --------------------------------------------------------------
    # Explicit ligand_resname OVERRIDES the standard-AA/water filter (so a ligand that happens to
    # use an AA-like 3-letter code can still be selected). Auto mode picks the largest non-water,
    # non-ion, non-protein het residue.
    want = ligand_resname.strip().upper() if ligand_resname else None
    best = None  # (n_heavy, chain_name, seqid_num, residue, heavy_atoms)
    for chain in model:
        for res in chain:
            nm = res.name.strip().upper()
            heavy = [a for a in res if not _is_hydrogen(a)]
            if want is not None:
                if nm != want or not heavy:
                    continue
            else:
                if nm in _STANDARD_AA or nm in _WATER:
                    continue
                if len(heavy) <= 1:
                    continue  # monatomic / diatomic -> ion or artifact, never the ligand
            if best is None or len(heavy) > best[0]:
                best = (len(heavy), chain.name, int(res.seqid.num),
                        _clean(getattr(res.seqid, "icode", "")), res, heavy)
    if best is None:
        raise ComplexSplitError(
            "No ligand residue found in the complex (looked for a non-water, non-ion HETATM "
            "residue with >1 heavy atom). If the ligand uses a standard-residue name, pass "
            "ligand_resname explicitly."
        )
    _n_heavy, lig_chain, lig_seqid, lig_icode, lig_res, lig_heavy = best
    lig_name = lig_res.name.strip().upper()
    # Identity of the residue to EXCLUDE from the receptor (chain, seqid, icode, resname) — handles
    # an AA-named ligand and disambiguates residues that share a seqid via insertion code.
    lig_key = (lig_chain, lig_seqid, lig_icode, lig_name)

    # ---- ligand pose PDBQT (single model, heavy atoms only) ------------------------------------
    pose_lines = ["MODEL 1"]
    for i, atom in enumerate(lig_heavy, start=1):
        el = (atom.element.name or "C").strip()
        aname = (atom.name or el)[:4]
        p = atom.pos
        # Exact PDB columns: serial 7-11, name 13-16, resName 18-20, chain 22, resSeq 23-26,
        # x/y/z 31-54, occ 55-60, temp 61-66, element 77-78 (parsed as the AutoDock type).
        pose_lines.append(
            "HETATM%5d %-4s %-3s A%4d    %8.3f%8.3f%8.3f  1.00  0.00          %2s"
            % (i, aname, "LIG", 1, p.x, p.y, p.z, el.rjust(2))
        )
    pose_lines.append("ENDMDL")
    Path(pose_pdbqt_out).write_text("\n".join(pose_lines) + "\n")

    # ---- protein-only receptor PDB (drop ligands + waters, incl. the selected ligand) ----------
    n_prot_atoms, n_prot_res = _write_protein_pdb(st, gemmi, receptor_pdb_out, exclude=lig_key)
    if n_prot_atoms == 0:
        raise ComplexSplitError("No protein (standard amino-acid) atoms found in the complex.")

    return {
        "ligand_resname": lig_name,
        "n_ligand_heavy": len(lig_heavy),
        "n_protein_atoms": n_prot_atoms,
        "n_protein_residues": n_prot_res,
    }


def _clean(s) -> str:
    """Normalize a gemmi char field: it uses '\\x00' (not '') for an absent altLoc/icode."""
    return (s or "").replace("\x00", "").strip()


def _is_hydrogen(atom) -> bool:
    try:
        if atom.is_hydrogen():
            return True
    except Exception:  # noqa: BLE001
        pass
    el = getattr(getattr(atom, "element", None), "name", "") or ""
    return el.strip().upper() == "H"


def _write_protein_pdb(st, gemmi, out_path, *, exclude=None) -> tuple[int, int]:
    """Write a protein-only PDB: keep standard-AA residues, drop ligands/waters/ions — and also
    drop the explicitly-selected ligand residue ``exclude`` = (chain_name, seqid_num, icode,
    resname), which covers a ligand that uses a standard-AA name.

    Emits ATOM lines by READ-ONLY iteration (no gemmi structure mutation, which can crash on some
    builds) using the same column layout as io.receptor.cif_to_pdb_lines, so pdb2gmx -ignh accepts
    it. Returns (n_atoms_kept, n_residues_kept).
    """
    out: list[str] = []
    serial = 0
    n_res = 0
    for chain in st[0]:
        wrote = False
        for res in chain:
            rn = res.name.strip().upper()
            if rn not in _STANDARD_AA:
                continue
            ric = _clean(getattr(res.seqid, "icode", ""))
            if exclude is not None and (chain.name, int(res.seqid.num), ric, rn) == exclude:
                continue  # the selected ligand happens to use an AA-like name
            n_res += 1
            cn = (chain.name or "A")[:1]
            resseq = int(res.seqid.num) % 10000
            icode = (ric or " ")[:1]  # preserve insertion code in the emitted record
            for atom in res:
                # Keep a single conformer: skip alternate locations other than the primary (''/'A'),
                # so pdb2gmx doesn't see duplicate atoms for the same residue position.
                alt = _clean(getattr(atom, "altloc", ""))
                if alt and alt.upper() != "A":
                    continue
                serial += 1
                element = (atom.element.name or "".join(c for c in atom.name if c.isalpha())[:1] or "C").strip()
                aname = atom.name or element
                # 1-char element names indent one space (PDB atom-name column alignment).
                name_field = (" " + aname).ljust(4) if (len(element) == 1 and len(aname) < 4) else aname[:4].ljust(4)
                p = atom.pos
                out.append(
                    f"{'ATOM':<6}{serial % 100000:>5} {name_field}{' '}{rn[:3]:>3} "
                    f"{cn}{resseq:>4}{icode}   {p.x:>8.3f}{p.y:>8.3f}{p.z:>8.3f}"
                    f"{1.0:>6.2f}{0.0:>6.2f}          {element:>2}"
                )
                wrote = True
        if wrote:
            out.append("TER")
    out.append("END")
    Path(out_path).write_text("\n".join(out) + "\n")
    return serial, n_res
