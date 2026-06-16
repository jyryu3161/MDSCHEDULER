"""Unit tests for bound-window detection, per-residue contacts/H-bonds, and the
bound-window -> md.xtc frame mapping used by MM/PBSA.

These cover the seams that are easy to get subtly wrong: leading-segment detection on the
ligand RMSD series, the geometric contact/H-bond proxy over a frame subset, and the
fraction-based time->frame conversion (1-based inclusive)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mdworker.pipeline.steps.analyze_md import _bound_window, _residue_contacts
from mdworker.pipeline.steps.mmpbsa import _bound_endframe


# ── _bound_window ────────────────────────────────────────────────────────────
def test_bound_window_partial_dissociation():
    times = [round(i * 0.5, 4) for i in range(10)]
    lig = [0.0, 1.2, 2.1, 3.0, 4.4, 6.7, 12.0, 20.0, 30.0, 41.0]
    bw = _bound_window(lig, times, 5.0)
    assert bw["n_bound_frames"] == 5          # frames 0..4 (< 5 Å), frame 5 breaks
    assert bw["end_ns"] == 2.0
    assert bw["fully_bound"] is False


def test_bound_window_fully_bound():
    bw = _bound_window([0.0, 1.0, 2.0], [0.0, 1.0, 2.0], 5.0)
    assert bw["n_bound_frames"] == 3
    assert bw["fully_bound"] is True


def test_bound_window_empty_is_not_fully_bound():
    bw = _bound_window([], [], 5.0)
    assert bw["n_bound_frames"] == 0
    assert bw["fully_bound"] is False


def test_bound_window_frame0_already_unbound():
    # If even the first frame exceeds the cutoff there is no bound segment.
    bw = _bound_window([9.0, 1.0], [0.0, 1.0], 5.0)
    assert bw["n_bound_frames"] == 0
    assert bw["fully_bound"] is False


# ── _residue_contacts ────────────────────────────────────────────────────────
def _atom(name, chain, resseq, resname, element):
    return SimpleNamespace(name=name, chain=chain, resseq=resseq, resname=resname,
                           element=element, is_ligand=(resname == "LIG"))


def test_residue_contacts_close_vs_far():
    atoms = [
        _atom("N", "A", 1, "CYS", "N"), _atom("CA", "A", 1, "CYS", "C"),
        _atom("N", "A", 2, "ILE", "N"), _atom("CA", "A", 2, "ILE", "C"),
        _atom("O1", "A", 900, "LIG", "O"),
    ]
    prot_idx = np.arange(4)
    lig_idx = np.array([4])
    f = np.array([
        [10, 10, 10], [11, 10, 10],     # CYS1 far
        [0, 0, 2.0], [0, 1, 2.0],       # ILE2 near (N at 2.0 Å from ligand O)
        [0, 0, 0],                       # ligand O at origin
    ], dtype=float)
    res = _residue_contacts(atoms, [f, f.copy()], lig_idx, prot_idx, [0, 1])
    assert len(res) == 1                 # CYS1 (far) omitted
    assert res[0]["resname"] == "ILE" and res[0]["resnum"] == 2
    assert res[0]["contact_frequency"] == 1.0
    assert res[0]["hbond_mean"] == 1.0   # one polar(N)-polar(O) pair within 3.5 Å


def test_residue_contacts_empty_inputs():
    assert _residue_contacts([], [], np.array([]), np.array([]), []) == []


# ── _bound_endframe (time -> md.xtc 1-based frame) ───────────────────────────
def _ctx_with_summary(tmp_path: Path, bound_window: dict, md_len: float):
    ana = tmp_path / "analysis"
    ana.mkdir(parents=True, exist_ok=True)
    (ana / "summary.json").write_text(json.dumps({"md_length_ns": md_len, "bound_window": bound_window}))
    return SimpleNamespace(analysis_dir=ana)


def test_bound_endframe_fraction_mapping(tmp_path):
    ctx = _ctx_with_summary(tmp_path, {"end_ns": 2.0, "fully_bound": False}, 50.0)
    settings = SimpleNamespace(trajectory_output_ps=10.0)
    # 2 ns of 50 ns -> frac 0.04; n_xtc=5001 -> floor(0.04*5000)+1 = 201
    endframe, end_ns = _bound_endframe(ctx, settings, {"completed_ns": 50.0}, 5001)
    assert endframe == 201
    assert end_ns == 2.0


def test_bound_endframe_fully_bound_takes_all(tmp_path):
    ctx = _ctx_with_summary(tmp_path, {"end_ns": 50.0, "fully_bound": True}, 50.0)
    settings = SimpleNamespace(trajectory_output_ps=10.0)
    endframe, _ = _bound_endframe(ctx, settings, {"completed_ns": 50.0}, 5001)
    assert endframe == 5001


def test_bound_endframe_tps_fallback_without_xtc_count(tmp_path):
    ctx = _ctx_with_summary(tmp_path, {"end_ns": 2.0, "fully_bound": False}, 50.0)
    settings = SimpleNamespace(trajectory_output_ps=10.0)
    # no xtc count -> round(2000/10)+1 = 201
    endframe, _ = _bound_endframe(ctx, settings, {"completed_ns": 50.0}, 0)
    assert endframe == 201


def test_bound_endframe_absent_window(tmp_path):
    ana = tmp_path / "analysis"
    ana.mkdir(parents=True)
    (ana / "summary.json").write_text(json.dumps({"md_length_ns": 50.0}))
    ctx = SimpleNamespace(analysis_dir=ana)
    settings = SimpleNamespace(trajectory_output_ps=10.0)
    endframe, end_ns = _bound_endframe(ctx, settings, {"completed_ns": 50.0}, 5001)
    assert endframe is None and end_ns is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
