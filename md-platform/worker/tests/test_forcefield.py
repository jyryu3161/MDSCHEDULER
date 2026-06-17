"""Tier 3 unit tests — force-field/water preflight resolution, the configurable equilibration/box
knobs, and the MM/GBSA leaprc mapping. These cover the decision logic WITHOUT a live GROMACS:
the ff19SB+OPC default must resolve to itself when the port is installed and fall back to
amber14sb+tip3p when it is not, so a plain GROMACS install still runs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mdworker.config import Settings
from mdworker.pipeline.engine.gromacs import (
    GromacsEngine, _ff_top_dirs, _ff_water_available, _solvent_box, _water_has_vsites)
from mdworker.pipeline.steps.mmpbsa import _protein_leaprc


class _FakeCtx:
    """Minimal JobContext stand-in capturing info/warning log lines."""

    def __init__(self) -> None:
        self.infos: list = []
        self.warnings: list = []

    def info(self, step, msg):
        self.infos.append((step, msg))

    def warning(self, step, msg):
        self.warnings.append((step, msg))


def _engine(**overrides) -> GromacsEngine:
    base = dict(protein_force_field="ff19SB", water_model="opc",
                protein_force_field_fallback="amber14sb", water_model_fallback="tip3p",
                forcefield_autofallback=True)
    base.update(overrides)
    return GromacsEngine(Settings(**base))


def _make_ff(top: Path, name: str, waters: list[str] | None) -> Path:
    """Create a fake ``<name>.ff`` dir; if ``waters`` is given, write a watermodels.dat listing them."""
    ffd = top / f"{name}.ff"
    ffd.mkdir(parents=True, exist_ok=True)
    (ffd / "forcefield.itp").write_text("; fake ff\n")
    if waters is not None:
        lines = ["; water models", *[f"{w}   fake {w} model" for w in waters]]
        (ffd / "watermodels.dat").write_text("\n".join(lines) + "\n")
    return ffd


# ── _ff_water_available ──────────────────────────────────────────────────────
def test_ff19sb_with_opc_available_when_listed(tmp_path):
    _make_ff(tmp_path, "ff19SB", ["opc", "tip3p"])
    assert _ff_water_available([tmp_path], "ff19SB", "opc") is True


def test_ff19sb_opc_unavailable_when_not_listed(tmp_path):
    # ff dir exists but watermodels.dat omits OPC (OPC is not a built-in) -> unavailable.
    _make_ff(tmp_path, "ff19SB", ["tip3p", "tip4p"])
    assert _ff_water_available([tmp_path], "ff19SB", "opc") is False


def test_missing_ff_dir_is_unavailable(tmp_path):
    _make_ff(tmp_path, "amber14sb", ["tip3p"])
    assert _ff_water_available([tmp_path], "ff19SB", "opc") is False


def test_builtin_water_available_without_watermodels_dat(tmp_path):
    # tip3p is a gmx pdb2gmx built-in: available whenever the ff dir exists, even with no
    # watermodels.dat (that file only drives the interactive `select` list for custom models).
    _make_ff(tmp_path, "amber14sb", None)
    assert _ff_water_available([tmp_path], "amber14sb", "tip3p") is True


def test_builtin_water_available_even_if_not_listed(tmp_path):
    _make_ff(tmp_path, "amber14sb", ["tip4p"])  # watermodels.dat without tip3p
    assert _ff_water_available([tmp_path], "amber14sb", "tip3p") is True


def test_fallback_pair_available(tmp_path):
    _make_ff(tmp_path, "amber14sb", ["tip3p", "tip4p", "spce"])
    assert _ff_water_available([tmp_path], "amber14sb", "tip3p") is True


def test_ff_found_in_any_of_multiple_dirs(tmp_path):
    d1 = tmp_path / "a"; d2 = tmp_path / "b"
    d1.mkdir(); d2.mkdir()
    _make_ff(d2, "ff19SB", ["opc"])
    assert _ff_water_available([d1, d2], "ff19SB", "opc") is True


def test_ff_top_dirs_picks_up_gmxlib(tmp_path, monkeypatch):
    real = tmp_path / "topdir"
    real.mkdir()
    monkeypatch.setenv("GMXLIB", f"{real}{':'}/nonexistent/xyz")
    dirs = _ff_top_dirs(None)
    assert real in dirs
    assert all(d.is_dir() for d in dirs)  # nonexistent entries are dropped


# ── GromacsEngine._ff_water (resolve vs fallback) ────────────────────────────
def _isolate_gmx(monkeypatch):
    """Make FF preflight depend ONLY on the cwd we pass: clear GMX env + stub away any real gmx
    so _ff_top_dirs cannot pick up a system GROMACS install on the test host/CI."""
    monkeypatch.delenv("GMXLIB", raising=False)
    monkeypatch.delenv("GMXDATA", raising=False)
    monkeypatch.setattr("mdworker.pipeline.engine.gromacs.shutil.which", lambda name: None)


def test_ff_water_uses_requested_pair_when_available(tmp_path, monkeypatch):
    _isolate_gmx(monkeypatch)
    _make_ff(tmp_path, "ff19SB", ["opc", "tip3p"])
    eng = _engine()
    ctx = _FakeCtx()
    ff, water = eng._ff_water(ctx, cwd=tmp_path)
    assert (ff, water) == ("ff19SB", "opc")
    assert not ctx.warnings
    # Sidecar recorded for the MM/GBSA step, and the choice is memoized.
    import json
    rec = json.loads((tmp_path / "forcefield.json").read_text())
    assert rec["protein_force_field"] == "ff19SB" and rec["water_model"] == "opc"
    assert eng._ff_water(ctx, cwd=tmp_path) == ("ff19SB", "opc")


def test_ff_water_falls_back_when_missing(tmp_path, monkeypatch):
    _isolate_gmx(monkeypatch)
    _make_ff(tmp_path, "amber14sb", ["tip3p"])  # ff19SB port NOT installed
    eng = _engine(forcefield_autofallback=True)
    ctx = _FakeCtx()
    ff, water = eng._ff_water(ctx, cwd=tmp_path)
    assert (ff, water) == ("amber14sb", "tip3p")
    assert ctx.warnings and "falling back" in ctx.warnings[-1][1]


def test_ff_water_strict_mode_keeps_requested_pair(tmp_path, monkeypatch):
    _isolate_gmx(monkeypatch)
    _make_ff(tmp_path, "amber14sb", ["tip3p"])  # ff19SB missing, but autofallback off
    eng = _engine(forcefield_autofallback=False)
    ctx = _FakeCtx()
    ff, water = eng._ff_water(ctx, cwd=tmp_path)
    assert (ff, water) == ("ff19SB", "opc")  # attempted as-is so gmx fails loudly
    assert ctx.warnings


def test_ff_water_no_topdirs_trusts_request(tmp_path, monkeypatch):
    # No GMXLIB/GMXDATA, no gmx on PATH, no cwd ff dirs -> can't preflight -> don't downgrade.
    _isolate_gmx(monkeypatch)
    eng = _engine()
    ctx = _FakeCtx()
    ff, water = eng._ff_water(ctx, cwd=None)
    assert (ff, water) == ("ff19SB", "opc")
    assert not ctx.warnings  # informational only, not a fallback warning


# ── config defaults + env overrides ──────────────────────────────────────────
def test_config_defaults_to_ff19sb_opc_with_fallback(monkeypatch):
    for var in ("PROTEIN_FORCE_FIELD", "WATER_MODEL", "PROTEIN_FORCE_FIELD_FALLBACK",
                "WATER_MODEL_FALLBACK", "BOX_PADDING_NM", "NVT_STEPS", "NPT_STEPS",
                "FORCEFIELD_AUTOFALLBACK"):
        monkeypatch.delenv(var, raising=False)
    from mdworker.config import load_settings
    s = load_settings()
    assert s.protein_force_field == "amber19sb"  # GROMACS port dir name (ff19SB ships as amber19sb.ff)
    assert s.water_model == "opc"
    assert s.protein_force_field_fallback == "amber14sb"
    assert s.water_model_fallback == "tip3p"
    assert s.forcefield_autofallback is True
    assert s.box_padding_nm == 1.2
    assert s.nvt_steps == 50000
    assert s.npt_steps == 125000


def test_config_env_overrides(monkeypatch):
    monkeypatch.setenv("PROTEIN_FORCE_FIELD", "amber14sb")
    monkeypatch.setenv("WATER_MODEL", "tip3p")
    monkeypatch.setenv("BOX_PADDING_NM", "1.0")
    monkeypatch.setenv("NVT_STEPS", "25000")
    monkeypatch.setenv("NPT_STEPS", "60000")
    monkeypatch.setenv("FORCEFIELD_AUTOFALLBACK", "false")
    from mdworker.config import load_settings
    s = load_settings()
    assert s.protein_force_field == "amber14sb"
    assert s.water_model == "tip3p"
    assert s.box_padding_nm == 1.0
    assert s.nvt_steps == 25000
    assert s.npt_steps == 60000
    assert s.forcefield_autofallback is False


# ── MM/GBSA leaprc mapping ───────────────────────────────────────────────────
# ── solvent box selection by water-model site count ──────────────────────────
@pytest.mark.parametrize("water,box", [
    ("tip3p", "spc216.gro"), ("spc", "spc216.gro"), ("spce", "spc216.gro"),
    ("opc3", "spc216.gro"),                    # OPC3 is a 3-point model
    ("opc", "tip4p.gro"), ("OPC", "tip4p.gro"),  # 4-point -> must NOT use spc216 (the real bug)
    ("tip4p", "tip4p.gro"), ("tip4pew", "tip4p.gro"),
    ("tip5p", "tip5p.gro"),
    ("", "spc216.gro"),                        # default
])
def test_solvent_box_matches_water_site_count(water, box):
    assert _solvent_box(water) == box


@pytest.mark.parametrize("water,vsites", [
    ("tip3p", False), ("spc", False), ("spce", False), ("opc3", False),
    ("opc", True), ("OPC", True), ("tip4p", True), ("tip4pew", True), ("tip5p", True),
    ("", False),
])
def test_water_has_vsites(water, vsites):
    # 4-/5-point water has a virtual site -> mdrun must NOT use `-update gpu`.
    assert _water_has_vsites(water) is vsites


@pytest.mark.parametrize("name,expected", [
    ("ff19SB", "leaprc.protein.ff19SB"),
    ("amber14sb", "oldff/leaprc.ff14SB"),
    ("ff14SB", "oldff/leaprc.ff14SB"),
    ("amber99sb-ildn", "oldff/leaprc.ff99SBildn"),
    ("something-unknown", "oldff/leaprc.ff14SB"),  # safe default
    ("", "oldff/leaprc.ff14SB"),
])
def test_protein_leaprc_mapping(name, expected):
    assert _protein_leaprc(name) == expected


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
