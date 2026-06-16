"""Fast unit tests for the docking engine-selection, adaptive blind-docking box, and smina
score parsing. Pure logic — no Vina/smina/obabel invocation (the real vina-vs-smina parity is
validated by an integration run, too slow/heavy for the unit suite)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mdworker.design import docking as D


def test_resolve_engine_valid_values(monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda name: "/usr/bin/smina" if name == "smina" else None)
    assert D.resolve_engine("auto") == "smina"      # smina present
    assert D.resolve_engine("vina") == "vina"
    assert D.resolve_engine("SMINA") == "smina"     # case-insensitive
    monkeypatch.setattr(D.shutil, "which", lambda name: None)
    assert D.resolve_engine("auto") == "vina"       # smina absent -> vina
    assert D.resolve_engine("") == "vina"           # empty -> auto -> vina here


def test_resolve_engine_accepts_gnina_passthrough(monkeypatch):
    # gnina passes through (opt-in); auto never picks it even when only gnina is installed.
    assert D.resolve_engine("gnina") == "gnina"
    monkeypatch.setattr(D.shutil, "which", lambda name: "/usr/bin/gnina" if name == "gnina" else None)
    assert D.resolve_engine("auto") == "vina"        # smina absent -> vina, never gnina


@pytest.mark.parametrize("bad", ["glide", "adcp", "haddock", "flexpepdock"])
def test_resolve_engine_rejects_unknown(bad):
    # Unknown / unsupported engines (incl. the peptide-into-protein tools) must raise, not
    # silently fall back to vina.
    with pytest.raises(ValueError):
        D.resolve_engine(bad)


def test_box_adaptive_margin_scales_with_length(tmp_path):
    # Minimal receptor PDBQT: two atoms 10 Å apart on x.
    pdbqt = tmp_path / "r.pdbqt"
    pdbqt.write_text(
        "ATOM      1  N   ALA A   1       0.000   0.000   0.000  0.00  0.00     0.000 N\n"
        "ATOM      2  N   ALA A   2      10.000   0.000   0.000  0.00  0.00     0.000 N\n"
    )
    _, box_short = D._box_from_pdbqt(pdbqt, margin=8.0, seq_len=2)
    _, box_long = D._box_from_pdbqt(pdbqt, margin=8.0, seq_len=20)
    # longer peptide -> larger margin -> larger box on the spanned axis
    assert box_long[0] > box_short[0]
    # capped: margin never exceeds 12 Å, so box_x <= extent(10) + 2*12 = 34
    assert box_long[0] <= 10 + 2 * 12 + 1e-6


def test_box_requires_coordinates(tmp_path):
    empty = tmp_path / "empty.pdbqt"
    empty.write_text("REMARK no atoms here\n")
    with pytest.raises(RuntimeError):
        D._box_from_pdbqt(empty)


def test_parse_smina_scores_minimized_affinity(tmp_path):
    out = tmp_path / "poses.pdbqt"
    out.write_text(
        "MODEL 1\nREMARK minimizedAffinity -7.30\nATOM ...\nENDMDL\n"
        "MODEL 2\nREMARK minimizedAffinity -6.10\nENDMDL\n"
    )
    assert D._parse_smina_scores(out) == [-7.3, -6.1]


def test_parse_smina_scores_vina_result_format(tmp_path):
    out = tmp_path / "poses.pdbqt"
    out.write_text(
        "MODEL 1\nREMARK VINA RESULT:    -8.20      0.000      0.000\nENDMDL\n"
        "MODEL 2\nREMARK VINA RESULT:    -7.00      1.234      2.345\nENDMDL\n"
    )
    assert D._parse_smina_scores(out) == [-8.2, -7.0]


def test_parse_smina_scores_raises_on_malformed_affinity(tmp_path):
    # A malformed affinity-prefixed REMARK is corruption, not a skippable line -> must raise,
    # so a partial/garbled score list can't silently feed the GA.
    out = tmp_path / "poses.pdbqt"
    out.write_text(
        "MODEL 1\nREMARK minimizedAffinity -7.30\nENDMDL\n"
        "MODEL 2\nREMARK minimizedAffinity NOT_A_NUMBER\nENDMDL\n"
    )
    with pytest.raises(RuntimeError):
        D._parse_smina_scores(out)


def test_parse_smina_scores_ignores_nonaffinity_remarks(tmp_path):
    # Non-affinity REMARK lines (SMILES, Name, etc.) are normal and must NOT raise.
    out = tmp_path / "poses.pdbqt"
    out.write_text(
        "REMARK SMILES CC(=O)O\nREMARK  Name = lig\n"
        "MODEL 1\nREMARK minimizedAffinity -6.10\nENDMDL\n"
    )
    assert D._parse_smina_scores(out) == [-6.1]


def test_safe_token_sanitizes_path_chars():
    # path separators / traversal must not survive into the workdir segment
    t = D._safe_token("../../etc/passwd")
    assert "/" not in t and ".." not in t and t


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
