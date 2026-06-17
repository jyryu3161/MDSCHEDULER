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


# ── evaluate_replicas (design-candidate replicas) ────────────────────────────
def test_evaluate_replicas_mock_is_deterministic_zero_sem():
    # Mock ΔG is deterministic -> all replicas identical -> mean = synthetic, SEM 0.
    agg = md_eval.evaluate_replicas("KCCIVYP", -4.0, n_replicas=3, engine="mock")
    assert agg["n"] == 3
    assert agg["dg"] == md_eval.synthetic_dg(-4.0)
    assert agg["sem"] == 0.0 and agg["std"] == 0.0


def test_evaluate_replicas_single_defaults():
    agg = md_eval.evaluate_replicas("KCCIVYP", -5.0, n_replicas=1, engine="mock")
    assert agg["n"] == 1 and agg["sem"] == 0.0
    assert agg["dg"] == md_eval.synthetic_dg(-5.0)


def test_evaluate_replicas_aggregates_varying_values(monkeypatch):
    # Stub evaluate() to return a different ΔG per replica (by workdir) -> real mean/SEM.
    seq = "KCCIVYP"
    vals = iter([-10.0, -12.0, -14.0])
    monkeypatch.setattr(md_eval, "evaluate", lambda *a, **k: next(vals))
    agg = md_eval.evaluate_replicas(seq, -4.0, n_replicas=3, engine="gromacs")
    assert agg["n"] == 3
    assert agg["dg"] == -12.0
    assert agg["std"] == 2.0
    assert agg["sem"] == pytest.approx(1.155, abs=1e-3)
    assert agg["values"] == [-10.0, -12.0, -14.0]


def test_evaluate_replicas_uses_distinct_workdirs(tmp_path, monkeypatch):
    seen: list = []
    monkeypatch.setattr(md_eval, "evaluate",
                        lambda *a, workdir=None, **k: (seen.append(workdir), -3.0)[1])
    md_eval.evaluate_replicas("KCCIVYP", -3.0, n_replicas=2, engine="mock", workdir=tmp_path)
    assert seen[0] != seen[1]
    assert {p.name for p in seen} == {"rep_01", "rep_02"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
