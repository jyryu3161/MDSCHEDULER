"""Unit tests for the peptide-design GA + hybrid evaluator.

Uses a fast synthetic fitness landscape (docking/MD stand-ins keyed to a target sequence) so
it runs in milliseconds with no Vina/GROMACS. Validates: PyGAD integration + monotonic
best-so-far convergence, the docking→MD hybrid (MD evaluated far less than docking),
cross-generation memoization, and fixed-length enforcement.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mdworker.design import ga as GA
from mdworker.design.peptide import indices_to_sequence, sequence_to_indices

_TARGET = "IIWWYYP"


def _sim(seq: str) -> int:
    return sum(a == b for a, b in zip(seq, _TARGET))


def _landscape():
    dock_calls = {"seqs": []}
    md_calls = {"seqs": []}

    def dock_batch(seqs, gen):
        dock_calls["seqs"].extend(seqs)
        return {s: -(_sim(s) + 0.01 * gen) for s in seqs}

    def md_batch(seqs, gen):
        md_calls["seqs"].extend(seqs)
        return {s: -(1.8 * _sim(s) + 1.0) for s in seqs}

    return dock_batch, md_batch, dock_calls, md_calls


def test_ga_converges_and_curve_is_monotonic():
    dock, md, _, _ = _landscape()
    res = GA.run_ga(["KCCIVYP", "AAAAAAA", "GGGGGGG"], dock, md,
                    num_generations=8, population_size=10, top_k_md=2, random_seed=7)
    traj = [g.best_fitness for g in res.generations]
    assert traj == sorted(traj)                      # monotonic non-decreasing (best-so-far)
    assert [g.generation for g in res.generations] == sorted({g.generation for g in res.generations})
    # final best matches the last generation's recorded best-so-far
    assert abs(res.best_fitness - res.generations[-1].best_fitness) < 1e-9
    assert _sim(res.best_sequence) >= _sim("KCCIVYP")  # improved over the seeds


def test_hybrid_runs_md_far_less_than_docking():
    dock, md, dc, mc = _landscape()
    GA.run_ga(["KCCIVYP", "AAAAAAA"], dock, md,
              num_generations=6, population_size=10, top_k_md=2, random_seed=3)
    assert len(mc["seqs"]) < len(dc["seqs"])          # MD is the expensive minority
    # MD only ever runs on <= top_k unique sequences per generation
    assert len(set(mc["seqs"])) <= len(set(dc["seqs"]))


def test_docking_is_memoized_across_generations():
    dock, md, dc, _ = _landscape()
    GA.run_ga(["KCCIVYP", "AAAAAAA"], dock, md,
              num_generations=8, population_size=8, top_k_md=2, random_seed=11)
    # each unique sequence is docked exactly once despite recurring across generations
    assert len(dc["seqs"]) == len(set(dc["seqs"]))


def test_best_candidate_uses_md_fitness_when_refined():
    dock, md, _, _ = _landscape()
    res = GA.run_ga(["KCCIVYP", "AAAAAAA", "GGGGGGG"], dock, md,
                    num_generations=8, population_size=10, top_k_md=3, random_seed=7)
    best = next(c for c in res.candidates if c.sequence == res.best_sequence)
    # In this landscape refined fitness (1.8*sim+1) always exceeds non-refined (sim), so the
    # global best is necessarily MD-refined and its fitness is exactly -md_dg.
    assert best.refined and best.md_dg is not None
    assert abs(best.fitness - (-best.md_dg)) < 1e-9


def test_failed_docking_does_not_crash_and_is_deselected():
    bad = "AAAAAAA"  # this exact sequence always fails to dock

    def dock_batch(seqs, gen):
        return {s: (None if s == bad else -float(_sim(s) + 1)) for s in seqs}

    def md_batch(seqs, gen):
        return {s: -(1.5 * _sim(s) + 1.0) for s in seqs}

    res = GA.run_ga([bad, "IIWWYYP"], dock_batch, md_batch,
                    num_generations=3, population_size=6, top_k_md=2, random_seed=1)
    failed = next(c for c in res.candidates if c.sequence == bad)
    assert failed.docking_score is None
    assert failed.fitness == GA._FAILED_FITNESS and not failed.refined  # deselected
    assert res.best_sequence != bad


def test_small_population_runs_multiple_generations():
    # Regression for the keep_elitism>=num_parents_mating trap: at pop=2 PyGAD would otherwise
    # stop after generation 0. The clamp must keep evolving across all requested generations.
    dock, md, _, _ = _landscape()
    res = GA.run_ga(["AC", "GG"], dock, md, num_generations=4, population_size=2,
                    top_k_md=1, keep_elitism=2, random_seed=5)
    gens = [g.generation for g in res.generations]
    assert max(gens) >= 3, f"GA stopped early — only saw generations {gens}"


def test_mixed_length_initial_sequences_rejected():
    dock, md, _, _ = _landscape()
    with pytest.raises(ValueError):
        GA.run_ga(["AAAA", "IIWWYYP"], dock, md, num_generations=2, population_size=4)


def test_index_sequence_roundtrip_used_by_ga():
    seq = "IIWWYYP"
    assert indices_to_sequence(sequence_to_indices(seq)) == seq


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
