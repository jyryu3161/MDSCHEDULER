"""Peptide sequence <-> 3D structure, and the amino-acid alphabet the GA evolves over.

The GA gene is a fixed-length vector of indices into ``AA1`` (the 20 standard amino acids);
this module converts between that index vector, the one-letter sequence string, and a 3D PDB
structure (via PeptideBuilder) suitable for docking / MD preparation.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Sequence

# 20 standard amino acids (one-letter). Index in this tuple IS the GA gene value (0..19).
AA1 = ("A", "R", "N", "D", "C", "Q", "E", "G", "H", "I",
       "L", "K", "M", "F", "P", "S", "T", "W", "Y", "V")
_AA_INDEX = {a: i for i, a in enumerate(AA1)}


def sequence_to_indices(seq: str) -> List[int]:
    """One-letter peptide sequence -> list of AA indices (0..19). Raises on unknown residues."""
    seq = seq.strip().upper()
    try:
        return [_AA_INDEX[a] for a in seq]
    except KeyError as exc:  # noqa: TRY003
        raise ValueError(f"Sequence contains a non-standard amino acid: {exc.args[0]!r}") from exc


def indices_to_sequence(indices: Sequence[int]) -> str:
    """List/array of AA indices -> one-letter sequence string."""
    out = []
    for i in indices:
        idx = int(round(float(i)))
        if idx < 0 or idx >= len(AA1):
            raise ValueError(f"Amino-acid index out of range: {i}")
        out.append(AA1[idx])
    return "".join(out)


def build_peptide(sequence: str, out_pdb: Path, *, geometry: str = "extended") -> Path:
    """Build a 3D PDB for ``sequence`` with PeptideBuilder and write it to ``out_pdb``.

    ``geometry`` selects the backbone conformation: "extended" (phi/psi ~ -120/+140, the
    default — gives the compound room to dock along the chain) or "helix" (alpha-helical).
    Returns ``out_pdb``.
    """
    import warnings

    if geometry not in ("extended", "helix"):
        raise ValueError(f"Unsupported geometry {geometry!r}; expected 'extended' or 'helix'.")

    seq = sequence.strip().upper()
    if not seq:
        raise ValueError("Cannot build an empty peptide.")

    with warnings.catch_warnings():
        # PeptideBuilder/Biopython emit noisy deprecation + PDBConstruction warnings; suppress
        # them only for the duration of the build instead of mutating global warnings state.
        warnings.simplefilter("ignore")
        import Bio.PDB
        import PeptideBuilder
        from PeptideBuilder import Geometry

        def _geo(aa: str):
            g = Geometry.geometry(aa)
            if geometry == "helix":
                g.phi, g.psi_im1 = -57.8, -47.0
            else:  # extended beta-like; gentle so PeptideBuilder stays numerically stable
                g.phi, g.psi_im1 = -120.0, 140.0
            return g

        structure = PeptideBuilder.initialize_res(_geo(seq[0]))
        for aa in seq[1:]:
            structure = PeptideBuilder.add_residue(structure, _geo(aa))
        PeptideBuilder.add_terminal_OXT(structure)

        out_pdb = Path(out_pdb)
        out_pdb.parent.mkdir(parents=True, exist_ok=True)
        io_ = Bio.PDB.PDBIO()
        io_.set_structure(structure)
        io_.save(str(out_pdb))
    return out_pdb


def peptide_pdb_string(sequence: str, *, geometry: str = "extended") -> str:
    """Same as :func:`build_peptide` but returns the PDB text instead of writing a file."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = build_peptide(sequence, Path(td) / "pep.pdb", geometry=geometry)
        return p.read_text()
