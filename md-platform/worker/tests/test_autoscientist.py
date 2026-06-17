"""AutoScientist peptide-design orchestrator — logic test with docking/MD mocked.

Uses a synthetic fitness landscape (closer to a target string = better score) and stubs out the
real docking/MD tools, so it runs in milliseconds with no Vina/GROMACS/Gemini. Validates: seeds
bootstrap a champion, rounds run, the champion improves over seeds, the GA-compatible DesignResult
shape (generations/candidates) plus the AutoScientist artifacts block, candidate persistence, and
the graceful no-Gemini fallback (guided mutation)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mdworker.design import autoscientist as AS  # noqa: E402
from mdworker.design import docking as DK  # noqa: E402
from mdworker.design import md_eval as ME  # noqa: E402

_TARGET = "IIWWYYP"


def _sim(seq: str) -> int:
    return sum(a == b for a, b in zip(seq, _TARGET))


class _Reporter:
    """Minimal in-memory DesignReporter stand-in."""

    def __init__(self):
        self.status = None
        self.candidates = []
        self.result = None

    def log(self, *a, **k):
        pass

    def set_status(self, design_id, status, **k):
        self.status = status

    def set_progress(self, design_id, progress, **k):
        pass

    def record_candidates(self, design_id, rows):
        self.candidates.extend(rows)

    def set_result(self, design_id, **k):
        self.result = k

    def request_gpu(self, design_id):
        return None

    def release_gpu(self, design_id):
        pass

    def is_cancelled(self, design_id):
        return False


@pytest.fixture
def _mock_tools(monkeypatch):
    # Docking: score = -(sim+1); a unique pose/peptide path per sequence (no real files needed).
    def fake_dock(seq, ligand_pdbqt, gen_dir, **kw):
        return DK.DockResult(sequence=seq, score=-(float(_sim(seq)) + 1.0),
                             pose_pdbqt=f"/tmp/{seq}.pdbqt", peptide_pdb=f"/tmp/{seq}.pdb",
                             engine="vina", all_scores=[-(float(_sim(seq)) + 1.0)])

    def fake_md(sequence, docking_score, **kw):
        # MD ΔG slightly amplifies the docking signal (more negative = better); deterministic.
        return {"dg": round(-(1.8 * _sim(sequence) + 1.0), 3), "sem": 0.1, "std": 0.2, "n": 1,
                "values": [-(1.8 * _sim(sequence) + 1.0)]}

    monkeypatch.setattr(DK, "prepare_ligand", lambda compound, out: Path(out))
    monkeypatch.setattr(DK, "resolve_engine", lambda e: "vina")
    monkeypatch.setattr(DK, "dock_peptide_compound", fake_dock)
    monkeypatch.setattr(ME, "evaluate_replicas", fake_md)
    monkeypatch.setattr(ME, "gromacs_available", lambda: False)
    # Force the no-Gemini path so the test is hermetic (exercises the guided-mutation fallback).
    monkeypatch.setattr(AS, "_safe_generate_json", lambda *a, **k: None)


def test_autoscientist_runs_and_improves(tmp_path, _mock_tools):
    config = {
        "initial_sequences": ["KCCIVYP", "AAAAAAA", "GGGGGGG"],
        "compound_file": str(tmp_path / "lig.sdf"),
        "compound_name": "testlig",
        "num_generations": 4,       # rounds
        "population_size": 8,       # candidates / round
        "dock_oversample": 3,       # research directions
        "md_length_ns": 10,
        "n_replicas": 1,
        "eval_mode": "hybrid",
    }
    rep = _Reporter()
    result = AS.run_autoscientist("design_001", config, rep,
                                  {"STORAGE_ROOT": str(tmp_path), "MD_ENGINE": "mock",
                                   "DOCK_ENGINE": "vina", "REPORT_ENABLED": "false"})

    assert rep.status == "completed"
    assert result["best_sequence"] is not None
    # Champion improved over the best seed.
    assert _sim(result["best_sequence"]) >= _sim("KCCIVYP")
    # GA-compatible shape: monotone best-so-far convergence + every evaluated candidate.
    fits = [g["best_fitness"] for g in result["generations"]]
    assert fits == sorted(fits)
    assert len(result["candidates"]) >= 3
    # AutoScientist artifacts block.
    asb = result["autoscientist"]
    assert asb["n_directions"] == 3 and len(asb["directions"]) == 3
    assert "forum" in asb and asb["champion_recipe"]["sequence"] == result["best_sequence"]
    # Candidates were persisted to the reporter (DB-compatible rows).
    assert len(rep.candidates) >= 3
    assert rep.result["best_sequence"] == result["best_sequence"]


def test_autoscientist_cancellation(tmp_path, _mock_tools, monkeypatch):
    rep = _Reporter()
    monkeypatch.setattr(rep, "is_cancelled", lambda design_id: True)
    config = {"initial_sequences": ["KCCIVYP"], "compound_file": str(tmp_path / "l.sdf"),
              "num_generations": 3, "population_size": 4, "dock_oversample": 2}
    with pytest.raises(AS.AutoScientistCancelled):
        AS.run_autoscientist("design_002", config, rep,
                             {"STORAGE_ROOT": str(tmp_path), "MD_ENGINE": "mock", "REPORT_ENABLED": "false"})
    assert rep.status == "cancelled"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
