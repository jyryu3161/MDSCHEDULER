"""MD engine interface (CONTRACT §9).

An engine encapsulates the steps that differ between a real GROMACS run and the synthetic
mock run: structure preparation (pdb2gmx), ligand parameterization (acpype), and the MD
sequence (assemble -> box -> solvate -> genion -> EM -> NVT -> NPT -> production). The
pipeline steps call these methods; the engine reports progress/status through the supplied
JobContext.

Data classes here are plain dataclasses with no heavy dependencies so the module imports
cheaply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PreparedStructure:
    """Output of prepare_structure (receptor topology)."""

    topology_path: str
    structure_path: str  # processed .gro / .pdb
    posre_path: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LigandParameters:
    """Output of parameterize_ligand."""

    itp_path: Optional[str]
    atomtypes_itp_path: Optional[str]
    posre_path: Optional[str]
    gro_path: Optional[str]
    charge_method: str
    force_field: str
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MDResult:
    """Output of run_md (production trajectory + final structures)."""

    trajectory_pdb_path: str  # multi-MODEL PDB the 3D viewer loads
    final_gro_path: Optional[str]
    xtc_path: Optional[str]
    tpr_path: Optional[str]
    completed_ns: float
    ns_per_day: float
    n_frames: int
    frame_interval_ps: float
    extra: Dict[str, Any] = field(default_factory=dict)


class MDEngine:
    """Abstract engine. Concrete implementations: GromacsEngine, MockEngine."""

    name: str = "base"

    def __init__(self, settings) -> None:
        self.settings = settings

    # -- capability flags --------------------------------------------------------------
    @property
    def is_real(self) -> bool:
        return False

    # -- pipeline operations -----------------------------------------------------------
    def prepare_structure(self, ctx, *, receptor_file: str, hetatm_decisions: Dict[str, str]) -> PreparedStructure:
        raise NotImplementedError

    def parameterize_ligand(
        self, ctx, *, lig_ref_sdf: str, ligand_pdb: str, ligand_type: str
    ) -> LigandParameters:
        raise NotImplementedError

    def run_md(
        self,
        ctx,
        *,
        prepared: PreparedStructure,
        ligand: LigandParameters,
        ligand_pdb: str,
        md_length_ns: float,
        assigned_gpu: Optional[int],
    ) -> MDResult:
        raise NotImplementedError
