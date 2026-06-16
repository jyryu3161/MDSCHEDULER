"""Peptide-design subsystem.

A genetic-algorithm (PyGAD) loop that evolves peptide sequences toward strong binding of
a fixed target compound. Each generation:
  1. build a 3D structure for every candidate peptide          (peptide.build_peptide)
  2. blind-dock the compound against each peptide (AutoDock Vina) -> docking score
  3. take the top-k by docking score and refine them with MD + MM/PBSA -> ΔG (binding)
  4. fitness = -ΔG for the MD-refined elites, -docking_score for the rest (more negative
     binding energy => higher fitness => more "bind-worthy")

This hybrid keeps the expensive MD off the bulk of the population (docking screens first)
while still scoring the most promising candidates with the more rigorous metric. The GA gene
representation is a fixed-length vector of amino-acid indices (0..19); length is fixed per
design job (= the initial population's sequence length).
"""

from .peptide import AA1, build_peptide, indices_to_sequence, sequence_to_indices

__all__ = ["AA1", "build_peptide", "indices_to_sequence", "sequence_to_indices"]
