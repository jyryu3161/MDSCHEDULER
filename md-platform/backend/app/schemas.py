"""Pydantic v2 request/response DTOs for every API in CONTRACT §5/§6/§7."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Auth (§5 Auth)
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool
    role: str
    username: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=4)


class OkResponse(BaseModel):
    ok: bool = True


class UserMe(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str
    must_change_password: bool
    is_active: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# Validation report (§7)
# ---------------------------------------------------------------------------


class PoseEntry(BaseModel):
    index: int
    # Optional: a non-docked input (AlphaFold-predicted complex) has no docking score.
    docking_score: float | None = None


class AtomMapping(BaseModel):
    attempted: bool = False
    success: bool = False
    template_heavy_atoms: int | None = None
    pose_heavy_atoms: int | None = None
    molformula_template: str | None = None
    molformula_pose: str | None = None
    matched_atoms: int | None = None
    message: str = ""


class HetatmCandidate(BaseModel):
    resname: str
    count: int
    suggested: str


class ReceptorInfo(BaseModel):
    format: str | None = None
    chains: list[str] = Field(default_factory=list)
    n_residues: int = 0
    n_atoms: int = 0
    has_hetatm: bool = False


class ValidationReport(BaseModel):
    ok: bool
    input_type: str
    pose_count: int = 0
    poses: list[PoseEntry] = Field(default_factory=list)
    ligand_type_candidates: list[str] = Field(default_factory=list)
    chem_source: str = "none"
    atom_mapping: AtomMapping = Field(default_factory=AtomMapping)
    hetatm_candidates: list[HetatmCandidate] = Field(default_factory=list)
    receptor: ReceptorInfo = Field(default_factory=ReceptorInfo)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("atom_mapping", mode="before")
    @classmethod
    def _default_atom_mapping(cls, v):  # noqa: ANN001
        # The worker may omit atom_mapping (e.g. raw PDBQT) -> default to "not attempted".
        return AtomMapping() if v is None else v

    @field_validator("receptor", mode="before")
    @classmethod
    def _default_receptor(cls, v):  # noqa: ANN001
        # The worker emits receptor=null when no receptor was provided.
        return ReceptorInfo() if v is None else v


# ---------------------------------------------------------------------------
# Uploads (§5 Uploads)
# ---------------------------------------------------------------------------


class UploadResponse(BaseModel):
    upload_id: str
    pose_file: str | None = None
    chemistry_file: str | None = None
    receptor_file: str | None = None
    detected_pose_count: int = 0
    detected_input_type: str
    ligand_type_candidates: list[str] = Field(default_factory=list)
    hetatm_candidates: list[HetatmCandidate] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Jobs (§6 JobCreate, §5 Jobs)
# ---------------------------------------------------------------------------


class JobCreate(BaseModel):
    upload_id: str
    name: str | None = None
    ligand_type: Literal[
        "small_molecule", "peptide", "protein_partner", "cofactor", "unknown"
    ] = "small_molecule"
    ligand_chem_source: Literal["sdf", "mol2", "smiles", "meeko", "manual"] = "sdf"
    top_n_poses: int = Field(default=3, ge=1, le=50)
    # Independent MD replicas per pose (different random velocity seeds). Each replica runs the
    # full pipeline as its own subjob; results are aggregated to mean ± SEM across replicas.
    n_replicas: int = Field(default=1, ge=1, le=10)
    md_length_ns: int = Field(default=50, ge=1, le=10000)
    md_preset: Literal["quick", "standard", "extended", "custom"] = "standard"
    force_field: str = "ff19SB"
    ligand_force_field: str = "gaff2"
    water_model: str = "opc"
    box_type: Literal["dodecahedron", "cubic"] = "dodecahedron"
    salt_concentration: float = 0.15
    temperature: float = 300.0
    pressure: float = 1.0
    use_gpu: bool = True
    priority: Literal["low", "normal", "high"] = "normal"
    hetatm_decisions: dict[str, str] = Field(default_factory=dict)
    cif_options: dict[str, Any] = Field(default_factory=dict)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: int
    name: str
    input_type: str
    ligand_type: str
    status: str
    md_length_ns: int
    top_n_poses: int
    n_replicas: int = 1
    force_field: str
    ligand_force_field: str
    ligand_chem_source: str
    water_model: str
    salt_concentration: float
    temperature: float
    pressure: float
    box_type: str
    priority: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_path: str | None = None
    error_message: str | None = None


class SubJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str
    pose_index: int
    replica_index: int = 1
    docking_score: float
    status: str
    assigned_gpu: int | None = None
    progress: float
    completed_ns: float
    ns_per_day: float
    current_step: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_path: str | None = None
    error_message: str | None = None


class JobLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: str
    subjob_id: str | None = None
    level: str
    step: str
    message: str
    created_at: datetime


class ReplicaStat(BaseModel):
    """Summary statistics across MD replicas of one pose (n samples = n replicas, NOT frames)."""

    n: int = 0
    mean: float | None = None
    sem: float | None = None      # standard error of the mean = std / sqrt(n)
    std: float | None = None      # sample standard deviation (n-1)
    min: float | None = None
    max: float | None = None


class ReplicaResult(BaseModel):
    """One replica's binding numbers (None until that replica's MM/GBSA is available)."""

    replica_index: int
    subjob_id: str
    status: str
    gbsa_dg_kcal_mol: float | None = None
    pbsa_dg_kcal_mol: float | None = None
    pose_occupancy: float | None = None


class PoseReplicaAggregate(BaseModel):
    """Per-pose aggregate over its replicas: mean ± SEM of the relative binding score + occupancy."""

    pose_index: int
    n_replicas: int
    gbsa: ReplicaStat
    pbsa: ReplicaStat
    pose_occupancy: ReplicaStat
    replicas: list[ReplicaResult]


class JobDetail(BaseModel):
    job: JobOut
    subjobs: list[SubJobOut]
    logs: list[JobLogOut]
    # Populated only for multi-replica jobs; mean ± SEM of the binding score across replicas.
    replica_aggregates: list[PoseReplicaAggregate] = []


# ---------------------------------------------------------------------------
# Queue (§5 Queue)
# ---------------------------------------------------------------------------


class QueueItem(BaseModel):
    job_id: str
    subjob_id: str
    job_name: str
    user: str
    pose_index: int
    status: str
    queue_position: int | None = None
    assigned_gpu: int | None = None
    progress: float = 0.0
    completed_ns: float = 0.0
    md_length_ns: int = 0
    ns_per_day: float = 0.0
    rough_eta_seconds: float | None = None


class QueueResponse(BaseModel):
    items: list[QueueItem]
    running: list[QueueItem]


class PriorityUpdate(BaseModel):
    priority: Literal["low", "normal", "high"]


# ---------------------------------------------------------------------------
# GPU (§5 GPU)
# ---------------------------------------------------------------------------


class GpuStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    gpu_id: int
    name: str
    status: str
    utilization: float
    memory_used: float
    memory_total: float
    temperature: float
    assigned_subjob_id: str | None = None
    pool: Literal["md", "design", "excluded"] = "md"
    capacity: int = Field(default=1, ge=0)          # max concurrent subjobs on this GPU
    running_count: int = Field(default=0, ge=0)     # slots currently in use (0..capacity)
    updated_at: datetime


# ---------------------------------------------------------------------------
# Results (§5 Results)
# ---------------------------------------------------------------------------


class SubJobResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str
    pose_index: int
    docking_score: float
    status: str
    progress: float
    completed_ns: float
    ns_per_day: float
    result_path: str | None = None
    error_message: str | None = None
    analysis_summary: dict[str, Any] = Field(default_factory=dict)
    plots_available: list[str] = Field(default_factory=list)
    has_trajectory: bool = False
    has_movie: bool = False
    # Optional MM/PBSA & MM/GBSA binding free energy (ΔG, kcal/mol) when computed
    # (job compute_mmpbsa=true). None when not run. Keys: gbsa_dg_kcal_mol, pbsa_dg_kcal_mol,
    # method, frames.
    mmpbsa: dict[str, Any] | None = None
    # Optional per-residue ΔG decomposition (which peptide residues drive binding). None when
    # not computed. Keys: residues:[{resname,resnum,total_dg,vdw,eel,...}], hotspots:[...].
    per_residue: dict[str, Any] | None = None
    # Auto-detected bound window (analysis/summary.json bound_window): the leading segment
    # where the ligand stays in the pocket. Keys: start_ns, end_ns, n_bound_frames,
    # n_total_frames, fully_bound, criterion. None for older results.
    bound_window: dict[str, Any] | None = None
    # Unified binding-hotspot table merging per-residue ΔG (MM/PBSA) with contact frequency
    # and mean H-bonds (geometric, over the bound window) keyed by residue. Empty when neither
    # source exists. Rows: {residue, chain, resname, resnum, total_dg, vdw, eel,
    # contact_frequency, hbond_mean}.
    hotspots: list[dict[str, Any]] = Field(default_factory=list)


class JobResults(BaseModel):
    job: JobOut
    subjobs: list[SubJobResult]


class SubJobResultDetail(BaseModel):
    subjob: SubJobResult
    pose_comparison: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Peptide design (GA)
# ---------------------------------------------------------------------------


class DesignJobCreate(BaseModel):
    """Create a peptide-design GA job. The compound is supplied separately (file upload or
    SMILES) by the router; this is the JSON config carried alongside it."""

    name: str = Field(min_length=1, max_length=255)
    initial_sequences: list[str] = Field(min_length=1)   # all must share one length
    population_size: int = Field(default=10, ge=2, le=200)
    num_generations: int = Field(default=5, ge=1, le=100)
    top_k_md: int = Field(default=2, ge=1, le=50)
    md_length_ns: int = Field(default=10, ge=1, le=1000)
    # Independent MD replicas per evaluated candidate; fitness uses the mean ΔG (capped low
    # because GA cost already scales with population × generations × top_k_md).
    n_replicas: int = Field(default=1, ge=1, le=5)
    exhaustiveness: int = Field(default=8, ge=1, le=64)
    # Per-generation evaluation policy: "hybrid" (dock all -> MD top-k, efficient, default) or
    # "md_only" (MD every candidate, most accurate, most costly).
    eval_mode: Literal["hybrid", "md_only"] = "hybrid"
    # Docking engine for this run (selectable at GA launch): vina (default) | smina | gnina | auto.
    dock_engine: Literal["vina", "smina", "gnina", "auto"] = "vina"
    # SMILES is written to disk as compound.smi; cap it so it can't bypass the upload size limit.
    smiles: str | None = Field(default=None, max_length=10000)
    compound_name: str = Field(default="compound", max_length=255)  # matches DB String(255)

    @field_validator("initial_sequences")
    @classmethod
    def _validate_sequences(cls, seqs: list[str]) -> list[str]:
        cleaned = [s.strip().upper() for s in seqs if s and s.strip()]
        if not cleaned:
            raise ValueError("initial_sequences must contain at least one peptide.")
        valid_aa = set("ARNDCQEGHILKMFPSTWYV")
        lengths = {len(s) for s in cleaned}
        if len(lengths) != 1:
            raise ValueError(f"All initial sequences must share one length; got lengths {sorted(lengths)}.")
        for s in cleaned:
            bad = sorted(set(s) - valid_aa)
            if bad:
                raise ValueError(f"Sequence {s!r} has non-standard amino acids: {bad}.")
        return cleaned


class DesignCandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    generation: int
    sequence: str
    docking_score: float | None = None
    md_dg: float | None = None
    fitness: float = 0.0
    refined: bool = False


class DesignJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: int
    name: str
    status: str
    compound_name: str
    peptide_length: int
    population_size: int
    num_generations: int
    top_k_md: int
    md_length_ns: int
    n_replicas: int = 1
    eval_mode: str = "hybrid"
    dock_engine: str = "vina"
    current_generation: int
    progress: float
    assigned_gpu: int | None = None
    best_sequence: str | None = None
    best_fitness: float | None = None
    best_docking_score: float | None = None
    best_md_dg: float | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None


class DesignGenerationPoint(BaseModel):
    generation: int
    best_fitness: float
    best_sequence: str
    best_docking_score: float | None = None
    best_md_dg: float | None = None


class DesignJobDetail(BaseModel):
    job: DesignJobOut
    candidates: list[DesignCandidateOut] = Field(default_factory=list)   # leaderboard (best first)
    generations: list[DesignGenerationPoint] = Field(default_factory=list)  # convergence curve


# ---------------------------------------------------------------------------
# Dashboard (§5 Dashboard summary)
# ---------------------------------------------------------------------------


class DashboardSummary(BaseModel):
    total_jobs: int
    running_jobs: int
    queued_jobs: int
    completed_jobs: int
    failed_jobs: int
    gpus_available: int
    gpus_busy: int
    storage_used_gb: float
    storage_total_gb: float


# ---------------------------------------------------------------------------
# Internal worker -> backend (§5 Internal)
# ---------------------------------------------------------------------------


class InternalSubjobStatus(BaseModel):
    status: str | None = None
    current_step: str | None = None
    progress: float | None = None
    completed_ns: float | None = None
    ns_per_day: float | None = None
    assigned_gpu: int | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_path: str | None = None


class InternalJobStatus(BaseModel):
    status: str | None = None
    result_path: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class InternalLog(BaseModel):
    job_id: str
    subjob_id: str | None = None
    level: Literal["info", "warning", "error"] = "info"
    step: str = ""
    message: str


class InternalGpuAssign(BaseModel):
    subjob_id: str | None = None
    status: str


class InternalGpuRequest(BaseModel):
    subjob_id: str


class InternalGpuRequestResponse(BaseModel):
    gpu_id: int | None = None


class InternalGpuReleaseRequest(BaseModel):
    subjob_id: str
