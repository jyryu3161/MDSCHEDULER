"""Unit tests for the unified binding-hotspot merge in results.py.

Verifies that per-residue ΔG (MM/PBSA) and geometric contact/H-bond data are merged by
residue, that either source may be missing, and that rows are ordered ΔG-first
(most favorable) with ΔG-less rows ranked by contact frequency. Filesystem reads are
stubbed via storage.pose_dir so no real job tree is needed.

Run from the backend/ directory:  pytest -q app/tests/test_hotspots.py
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# Configure env BEFORE importing the app (settings are cached at import time).
_TMP = Path(tempfile.mkdtemp(prefix="mdplatform_hotspots_"))
os.environ.setdefault("STORAGE_ROOT", str(_TMP))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'test.db'}")
os.environ.setdefault("QUEUE_BACKEND", "local")
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest  # noqa: E402

from app.routers import results as R  # noqa: E402
from app.services import storage  # noqa: E402


def _pose(tmp_path: Path, per_residue=None, contacts=None) -> Path:
    ana = tmp_path / "analysis"
    ana.mkdir(parents=True, exist_ok=True)
    if per_residue is not None:
        (ana / "per_residue.json").write_text(json.dumps(per_residue))
    if contacts is not None:
        (ana / "residue_contacts.json").write_text(json.dumps(contacts))
    return tmp_path


_PER_RES = {"residues": [
    {"chain": "A", "resname": "ILE", "resnum": 4, "total_dg": -2.01, "vdw": -1.8, "eel": -0.2},
    {"chain": "A", "resname": "TYR", "resnum": 6, "total_dg": -1.38, "vdw": -1.1, "eel": -0.3},
    {"chain": "A", "resname": "CYS", "resnum": 2, "total_dg": 0.06, "vdw": 0.0, "eel": 0.1},
]}
_CONTACTS = {"residues": [
    {"chain": "A", "resname": "ILE", "resnum": 4, "contact_frequency": 0.95, "hbond_mean": 0.2},
    {"chain": "A", "resname": "TYR", "resnum": 6, "contact_frequency": 0.80, "hbond_mean": 1.1},
    {"chain": "A", "resname": "PRO", "resnum": 7, "contact_frequency": 0.40, "hbond_mean": 0.0},
]}


def test_merge_both_sources(tmp_path, monkeypatch):
    pose = _pose(tmp_path, _PER_RES, _CONTACTS)
    monkeypatch.setattr(storage, "pose_dir", lambda j, p: pose)
    rows = R._hotspots("job", 1)
    # ΔG rows first (ascending), then contact-only (PRO7) last.
    assert [r["residue"] for r in rows] == ["ILE4", "TYR6", "CYS2", "PRO7"]
    ile = rows[0]
    assert ile["total_dg"] == -2.01 and ile["contact_frequency"] == 0.95 and ile["hbond_mean"] == 0.2
    cys2 = next(r for r in rows if r["residue"] == "CYS2")
    assert cys2["contact_frequency"] is None and cys2["hbond_mean"] is None  # ΔG-only
    pro7 = rows[-1]
    assert pro7["total_dg"] is None and pro7["contact_frequency"] == 0.40   # contact-only


def test_merge_dg_only(tmp_path, monkeypatch):
    pose = _pose(tmp_path, _PER_RES, None)
    monkeypatch.setattr(storage, "pose_dir", lambda j, p: pose)
    rows = R._hotspots("job", 1)
    assert [r["residue"] for r in rows] == ["ILE4", "TYR6", "CYS2"]
    assert all(r["contact_frequency"] is None for r in rows)


def test_merge_contacts_only(tmp_path, monkeypatch):
    pose = _pose(tmp_path, None, _CONTACTS)
    monkeypatch.setattr(storage, "pose_dir", lambda j, p: pose)
    rows = R._hotspots("job", 1)
    # No ΔG anywhere -> ranked by contact frequency desc.
    assert [r["residue"] for r in rows] == ["ILE4", "TYR6", "PRO7"]
    assert all(r["total_dg"] is None for r in rows)


def test_merge_neither_source(tmp_path, monkeypatch):
    pose = _pose(tmp_path, None, None)
    monkeypatch.setattr(storage, "pose_dir", lambda j, p: pose)
    assert R._hotspots("job", 1) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
