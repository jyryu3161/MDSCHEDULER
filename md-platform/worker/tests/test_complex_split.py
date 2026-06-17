"""Unit tests for the complex (protein+ligand) splitter used by the complex-CIF input path.

Verifies: protein-only receptor keeps amino acids and drops the ligand + water; the ligand pose
is the ligand's HEAVY atoms only; the generated pose PDBQT round-trips through the existing
parse_pdbqt_models (so the unchanged downstream pipeline consumes it)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mdworker.io.complex import ComplexSplitError, split_complex  # noqa: E402
from mdworker.io.pdbqt import parse_pdbqt_models  # noqa: E402

# Synthetic complex: GLY + ALA peptide (chain A) + a 3-heavy-atom ligand "LIG" (+1 H) + a water.
_COMPLEX_PDB = """\
ATOM      1  N   GLY A   1       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  GLY A   1       1.000   0.000   0.000  1.00  0.00           C
ATOM      3  C   GLY A   1       2.000   0.000   0.000  1.00  0.00           C
ATOM      4  O   GLY A   1       3.000   0.000   0.000  1.00  0.00           O
ATOM      5  N   ALA A   2       0.000   1.000   0.000  1.00  0.00           N
ATOM      6  CA  ALA A   2       1.000   1.000   0.000  1.00  0.00           C
ATOM      7  C   ALA A   2       2.000   1.000   0.000  1.00  0.00           C
ATOM      8  O   ALA A   2       3.000   1.000   0.000  1.00  0.00           O
ATOM      9  CB  ALA A   2       1.000   2.000   0.000  1.00  0.00           C
HETATM   10  C1  LIG A 101       5.000   5.000   5.000  1.00  0.00           C
HETATM   11  O1  LIG A 101       6.000   5.000   5.000  1.00  0.00           O
HETATM   12  N1  LIG A 101       5.000   6.000   5.000  1.00  0.00           N
HETATM   13  H1  LIG A 101       5.500   6.500   5.000  1.00  0.00           H
HETATM   14  O   HOH A 201       9.000   9.000   9.000  1.00  0.00           O
END
"""


def _split(tmp_path, pdb_text=_COMPLEX_PDB, **kw):
    src = tmp_path / "complex.pdb"
    src.write_text(pdb_text)
    rec = tmp_path / "receptor.pdb"
    pose = tmp_path / "pose.pdbqt"
    meta = split_complex(src, rec, pose, **kw)
    return meta, rec.read_text(), pose


def test_split_extracts_ligand_and_protein(tmp_path):
    meta, receptor_txt, pose_path = _split(tmp_path)
    assert meta["ligand_resname"] == "LIG"
    assert meta["n_ligand_heavy"] == 3            # C1, O1, N1 (H1 excluded)
    assert meta["n_protein_residues"] == 2        # GLY, ALA

    # Receptor keeps protein, drops ligand + water.
    assert "GLY" in receptor_txt and "ALA" in receptor_txt
    assert "LIG" not in receptor_txt
    assert "HOH" not in receptor_txt

    # Pose round-trips through the existing PDBQT parser as a single pose, heavy atoms only.
    poses = parse_pdbqt_models(str(pose_path))
    assert len(poses) == 1
    assert poses[0]["docking_score"] is None      # no docking
    elements = sorted(a.element for a in poses[0]["heavy_atoms"])
    assert elements == ["C", "N", "O"]
    assert all(not a.is_hydrogen for a in poses[0]["heavy_atoms"])


def test_no_ligand_raises(tmp_path):
    protein_only = "\n".join(
        ln for ln in _COMPLEX_PDB.splitlines() if "LIG" not in ln and "HOH" not in ln
    ) + "\nEND\n"
    with pytest.raises(ComplexSplitError):
        _split(tmp_path, protein_only)


def test_explicit_ligand_resname_overrides(tmp_path):
    # Pick a specific residue by name even though another het exists; here LIG is the only one,
    # so explicit selection must still work and exclude it from the receptor.
    meta, receptor_txt, _ = _split(tmp_path, ligand_resname="LIG")
    assert meta["ligand_resname"] == "LIG"
    assert "LIG" not in receptor_txt


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
