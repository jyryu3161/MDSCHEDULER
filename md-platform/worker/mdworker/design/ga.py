"""Genetic-algorithm peptide design with PyGAD (hybrid docking → MD evaluation).

The GA evolves a fixed-length vector of amino-acid indices (0..19). Each generation:
  • dock every (unique, not-yet-seen) candidate against the target compound  → docking score
  • take the top-k of the generation by docking score and refine with MD+MM/PBSA → ΔG
  • fitness = -ΔG for the MD-refined elites, -docking_score for everyone else
    (more negative binding energy ⇒ higher fitness; PyGAD maximizes fitness)

Compute dispatch is injected by the caller via two *batch* callbacks so the backend can fan
work out across the GPU pool / queue and "split a generation's compute":

    dock_batch(sequences, generation)  -> {seq: docking_score (float, lower=better)}
    md_batch(sequences, generation)    -> {seq: md_dg       (float, lower=better)}

The module owns only the GA mechanics + the hybrid policy + per-candidate memoization; it has
no knowledge of Vina/GROMACS/queues. This keeps it unit-testable with fast stand-in callbacks.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional

from .peptide import AA1, indices_to_sequence, sequence_to_indices

DockBatch = Callable[[List[str], int], Dict[str, float]]
MdBatch = Callable[[List[str], int], Dict[str, float]]
ProgressCb = Callable[[dict], None]

# Fitness assigned to a candidate whose docking failed, so the GA strongly deselects it
# without crashing the whole generation.
_FAILED_FITNESS = -1.0e6


@dataclass
class CandidateEval:
    sequence: str
    generation: int
    docking_score: Optional[float] = None
    md_dg: Optional[float] = None
    fitness: float = _FAILED_FITNESS
    refined: bool = False              # True once MD-refined (fitness uses md_dg)


@dataclass
class GenerationRecord:
    generation: int
    population: List[str]
    elites: List[str]                 # the top-k chosen for MD refinement
    best_sequence: str
    best_fitness: float
    best_docking_score: Optional[float] = None
    best_md_dg: Optional[float] = None


@dataclass
class DesignResult:
    best_sequence: str
    best_fitness: float
    best_docking_score: Optional[float]
    best_md_dg: Optional[float]
    generations: List[GenerationRecord] = field(default_factory=list)
    candidates: List[CandidateEval] = field(default_factory=list)  # every evaluated candidate


class HybridEvaluator:
    """Owns the dock-all → MD-top-k hybrid policy and the cross-generation memoization."""

    def __init__(self, dock_batch: DockBatch, md_batch: MdBatch, top_k: int,
                 progress: Optional[ProgressCb] = None):
        self.dock_batch = dock_batch
        self.md_batch = md_batch
        self.top_k = max(1, int(top_k))
        self.progress = progress
        self.cache: Dict[str, CandidateEval] = {}
        self._records: Dict[int, GenerationRecord] = {}  # keyed by generation (dedup)

    @property
    def records(self) -> List[GenerationRecord]:
        """Per-generation records in generation order (one per generation)."""
        return [self._records[g] for g in sorted(self._records)]

    def _emit(self, **kw) -> None:
        if self.progress:
            self.progress(kw)

    def evaluate_population(self, sequences: List[str], generation: int) -> None:
        """Dock all unseen sequences, MD-refine the generation's top-k, fill ``self.cache``."""
        unique = list(dict.fromkeys(sequences))  # preserve order, drop duplicates

        # 1) dock the candidates we have not scored yet
        to_dock = [s for s in unique if s not in self.cache]
        if to_dock:
            self._emit(stage="docking", generation=generation, n=len(to_dock))
            scores = self.dock_batch(to_dock, generation) or {}
            for s in to_dock:
                sc = scores.get(s)
                self.cache[s] = CandidateEval(
                    sequence=s, generation=generation,
                    docking_score=(None if sc is None else float(sc)),
                    fitness=(_FAILED_FITNESS if sc is None else -float(sc)),
                )

        # 2) pick the generation's top-k by docking score (best = most negative); refine w/ MD
        scored = [s for s in unique if self.cache.get(s) and self.cache[s].docking_score is not None]
        scored.sort(key=lambda s: self.cache[s].docking_score)  # ascending: best first
        elites = [s for s in scored[:self.top_k] if not self.cache[s].refined]
        # Candidates whose state changed this generation (newly docked OR newly refined) — these
        # must be (re)persisted so an earlier-seen candidate refined now emits its ΔG/fitness.
        touched = set(to_dock)
        if elites:
            self._emit(stage="md", generation=generation, n=len(elites), elites=list(elites))
            dgs = self.md_batch(elites, generation) or {}
            for s in elites:
                dg = dgs.get(s)
                ce = self.cache[s]
                if dg is not None:
                    ce.md_dg = float(dg)
                    ce.fitness = -float(dg)   # refined fitness uses MD binding ΔG
                    ce.refined = True
                    touched.add(s)

        self._record_generation(generation, unique, scored[:self.top_k], touched)

    def _record_generation(self, generation: int, evaluated_this_gen: List[str],
                           elites: List[str], touched: set) -> None:
        # best-so-far across EVERYTHING evaluated through this generation (the cache retains
        # elites carried over by PyGAD, which the batched fitness func no longer re-passes), so
        # the per-generation curve is the monotonic convergence curve rather than just this
        # generation's freshly-evaluated subset.
        best = max(self.cache.values(), key=lambda c: c.fitness)
        rec = GenerationRecord(
            generation=generation, population=list(evaluated_this_gen), elites=list(elites),
            best_sequence=best.sequence, best_fitness=best.fitness,
            best_docking_score=best.docking_score, best_md_dg=best.md_dg,
        )
        self._records[generation] = rec  # dedup: one record per generation index
        # Candidates whose state changed this generation (docked or refined now), each tagged
        # with its OWN first-seen generation so the caller can upsert without rewriting it.
        gen_candidates = {
            s: {"generation": self.cache[s].generation, "docking_score": self.cache[s].docking_score,
                "md_dg": self.cache[s].md_dg, "fitness": self.cache[s].fitness,
                "refined": self.cache[s].refined}
            for s in touched if s in self.cache
        }
        self._emit(stage="generation_done", generation=generation,
                   best_sequence=best.sequence, best_fitness=best.fitness,
                   best_docking_score=best.docking_score, best_md_dg=best.md_dg,
                   candidates=gen_candidates)

    def fitness_of(self, sequence: str) -> float:
        ce = self.cache.get(sequence)
        return ce.fitness if ce else _FAILED_FITNESS


def run_ga(
    initial_sequences: List[str],
    dock_batch: DockBatch,
    md_batch: MdBatch,
    *,
    num_generations: int = 5,
    population_size: int = 10,
    top_k_md: int = 2,
    num_parents_mating: Optional[int] = None,
    mutation_percent_genes: float = 20.0,
    keep_elitism: int = 2,
    random_seed: int = 7,
    progress: Optional[ProgressCb] = None,
) -> DesignResult:
    """Run the design GA and return the best peptide plus the full per-generation history.

    ``initial_sequences`` defines the fixed peptide length (all must share one length) and
    seeds the first population; if fewer than ``population_size`` are given, the rest are
    filled with random sequences of the same length.
    """
    import numpy as np
    import pygad

    if not initial_sequences:
        raise ValueError("initial_sequences must be non-empty.")
    lengths = {len(s.strip()) for s in initial_sequences}
    if len(lengths) != 1:
        raise ValueError(f"All initial sequences must share one length; got {sorted(lengths)}.")
    seq_len = lengths.pop()
    num_genes = seq_len

    rng = np.random.default_rng(random_seed)
    seed_genes = [sequence_to_indices(s) for s in initial_sequences]
    while len(seed_genes) < population_size:
        seed_genes.append(list(rng.integers(0, len(AA1), size=num_genes)))
    initial_population = np.array([g[:num_genes] for g in seed_genes[:population_size]], dtype=int)

    evaluator = HybridEvaluator(dock_batch, md_batch, top_k=top_k_md, progress=progress)

    def fitness_func(ga_instance, solutions, solution_indices):
        # fitness_batch_size makes PyGAD pass the whole batch of solutions needing evaluation.
        seqs = [indices_to_sequence(sol) for sol in solutions]
        evaluator.evaluate_population(seqs, generation=ga_instance.generations_completed)
        return [evaluator.fitness_of(s) for s in seqs]

    parents = num_parents_mating or max(2, population_size // 2)
    ga = pygad.GA(
        num_generations=num_generations,
        num_parents_mating=parents,
        fitness_func=fitness_func,
        fitness_batch_size=population_size,
        initial_population=initial_population,
        gene_type=int,
        gene_space=list(range(len(AA1))),     # each gene is an AA index 0..19
        mutation_type="random",
        mutation_percent_genes=mutation_percent_genes,
        crossover_type="single_point",
        parent_selection_type="tournament",
        keep_elitism=min(keep_elitism, population_size),
        random_seed=random_seed,
        suppress_warnings=True,
    )
    ga.run()

    sol, sol_fitness, _ = ga.best_solution()
    best_seq = indices_to_sequence(sol)
    best_ce = evaluator.cache.get(best_seq)
    return DesignResult(
        best_sequence=best_seq,
        best_fitness=float(sol_fitness),
        best_docking_score=(best_ce.docking_score if best_ce else None),
        best_md_dg=(best_ce.md_dg if best_ce else None),
        generations=evaluator.records,
        candidates=list(evaluator.cache.values()),
    )


def result_to_dict(res: DesignResult) -> dict:
    """JSON-serializable view of a :class:`DesignResult` (for persistence / API)."""
    return {
        "best_sequence": res.best_sequence,
        "best_fitness": round(res.best_fitness, 4),
        "best_docking_score": res.best_docking_score,
        "best_md_dg": res.best_md_dg,
        "generations": [asdict(r) for r in res.generations],
        "candidates": [asdict(c) for c in res.candidates],
    }
