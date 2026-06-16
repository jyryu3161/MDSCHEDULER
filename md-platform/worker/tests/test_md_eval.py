"""Unit tests for the design MD-refinement evaluator (md_eval): mock ΔG determinism and the
graceful fallback when the real GROMACS path raises (a candidate must never crash the GA)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mdworker.design import md_eval


def test_synthetic_dg_is_deterministic_and_anchored():
    # ΔG ≈ slope*dock - offset; more negative dock -> more negative (stronger) ΔG.
    a = md_eval.synthetic_dg(-3.0)
    b = md_eval.synthetic_dg(-5.0)
    assert a == md_eval.synthetic_dg(-3.0)   # deterministic
    assert b < a                              # stronger docking -> stronger ΔG


def test_mock_engine_uses_synthetic(monkeypatch):
    assert md_eval.evaluate("KCCIVYP", -4.0, engine="mock") == md_eval.synthetic_dg(-4.0)


def test_gromacs_unavailable_falls_back_to_mock(monkeypatch):
    monkeypatch.setattr(md_eval, "gromacs_available", lambda: False)
    logs = []
    dg = md_eval.evaluate("KCCIVYP", -4.0, engine="gromacs", log=logs.append)
    assert dg == md_eval.synthetic_dg(-4.0)
    assert any("mock" in m.lower() for m in logs)


def test_gromacs_exception_falls_back_without_reraise(monkeypatch):
    # A real-MD failure for one candidate must fall back to the mock ΔG, not propagate.
    monkeypatch.setattr(md_eval, "gromacs_available", lambda: True)

    def _boom(*a, **k):
        raise RuntimeError("simulated MD blow-up")

    monkeypatch.setattr(md_eval, "_gromacs_dg", _boom)
    logs = []
    dg = md_eval.evaluate("KCCIVYP", -4.0, engine="gromacs", workdir=None, log=logs.append)
    assert dg == md_eval.synthetic_dg(-4.0)
    assert any("fail" in m.lower() or "mock" in m.lower() for m in logs)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
