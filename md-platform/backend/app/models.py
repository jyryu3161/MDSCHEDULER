"""SQLAlchemy models (CONTRACT §2) and string-constant enums (CONTRACT §4).

Enums are deliberately plain string-constant containers (not Python ``enum.Enum``)
so the stored column values are exactly the contract strings and mirror cleanly
to ``frontend/src/types.ts``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    """Timezone-aware UTC now (stored naive-UTC in DB but produced as aware)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Enums (CONTRACT §4) — string constants
# ---------------------------------------------------------------------------


class JobStatus:
    UPLOADED = "uploaded"
    VALIDATING = "validating"
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING_EM = "running_em"
    RUNNING_NVT = "running_nvt"
    RUNNING_NPT = "running_npt"
    RUNNING_MD = "running_md"
    ANALYZING = "analyzing"
    RENDERING = "rendering"
    PACKAGING = "packaging"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ALL = (
        UPLOADED,
        VALIDATING,
        QUEUED,
        PREPARING,
        RUNNING_EM,
        RUNNING_NVT,
        RUNNING_NPT,
        RUNNING_MD,
        ANALYZING,
        RENDERING,
        PACKAGING,
        COMPLETED,
        FAILED,
        CANCELLED,
    )
    # Statuses that count as "actively running" for queue/dashboard.
    RUNNING_SET = (
        PREPARING,
        RUNNING_EM,
        RUNNING_NVT,
        RUNNING_NPT,
        RUNNING_MD,
        ANALYZING,
        RENDERING,
        PACKAGING,
    )
    TERMINAL_SET = (COMPLETED, FAILED, CANCELLED)


class GpuStatusEnum:
    AVAILABLE = "available"
    BUSY = "busy"
    DISABLED = "disabled"
    MAINTENANCE = "maintenance"
    ERROR = "error"

    ALL = (AVAILABLE, BUSY, DISABLED, MAINTENANCE, ERROR)


class GpuPool:
    """Which workload a GPU is reserved for (CONTRACT §2 gpustatus.pool)."""

    MD = "md"             # regular docking-result MD jobs
    DESIGN = "design"     # peptide-design (GA) MD evaluations
    EXCLUDED = "excluded"  # unmanaged — left free for other use, never scheduled

    ALL = (MD, DESIGN, EXCLUDED)


class LigandType:
    SMALL_MOLECULE = "small_molecule"
    PEPTIDE = "peptide"
    PROTEIN_PARTNER = "protein_partner"
    COFACTOR = "cofactor"
    UNKNOWN = "unknown"

    ALL = (SMALL_MOLECULE, PEPTIDE, PROTEIN_PARTNER, COFACTOR, UNKNOWN)


class ChemSource:
    SDF = "sdf"
    MOL2 = "mol2"
    SMILES = "smiles"
    MEEKO = "meeko"
    MANUAL = "manual"

    ALL = (SDF, MOL2, SMILES, MEEKO, MANUAL)


class PlotType:
    RMSD = "rmsd"
    RMSF = "rmsf"
    RG = "rg"
    SASA = "sasa"
    HBOND = "hbond"
    ENERGY = "energy"
    LIGAND_RMSD = "ligand_rmsd"
    CONTACT_MAP = "contact_map"
    PER_RESIDUE = "per_residue"  # MM/PBSA per-residue ΔG decomposition (binding hotspots)

    ALL = (RMSD, RMSF, RG, SASA, HBOND, ENERGY, LIGAND_RMSD, CONTACT_MAP, PER_RESIDUE)


class InputType:
    PDBQT = "pdbqt"
    CIF = "cif"
    PDB = "pdb"
    MIXED = "mixed"

    ALL = (PDBQT, CIF, PDB, MIXED)


class Priority:
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"

    ALL = (LOW, NORMAL, HIGH)
    ORDER = {HIGH: 0, NORMAL: 1, LOW: 2}


class Role:
    ADMIN = "admin"
    USER = "user"


class LogLevel:
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    ALL = (INFO, WARNING, ERROR)


# ---------------------------------------------------------------------------
# Tables (CONTRACT §2)
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(150), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default=Role.USER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    input_type: Mapped[str] = mapped_column(String(20), nullable=False)
    ligand_type: Mapped[str] = mapped_column(String(30), default=LigandType.UNKNOWN, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.UPLOADED, nullable=False, index=True)
    md_length_ns: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    top_n_poses: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    # Independent MD replicas per pose (different random velocity seeds), aggregated to mean ± SEM.
    n_replicas: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    force_field: Mapped[str] = mapped_column(String(50), default="amber14sb", nullable=False)
    ligand_force_field: Mapped[str] = mapped_column(String(50), default="gaff2", nullable=False)
    ligand_chem_source: Mapped[str] = mapped_column(String(20), default=ChemSource.SDF, nullable=False)
    water_model: Mapped[str] = mapped_column(String(20), default="tip3p", nullable=False)
    salt_concentration: Mapped[float] = mapped_column(Float, default=0.15, nullable=False)
    temperature: Mapped[float] = mapped_column(Float, default=300.0, nullable=False)
    pressure: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    box_type: Mapped[str] = mapped_column(String(20), default="dodecahedron", nullable=False)
    priority: Mapped[str] = mapped_column(String(10), default=Priority.NORMAL, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class SubJob(Base):
    __tablename__ = "subjobs"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("jobs.id"), nullable=False, index=True)
    pose_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 1-based replica number within a pose (1 when a job has a single replica). Subjobs sharing a
    # (job_id, pose_index) are independent MD replicas aggregated to mean ± SEM.
    replica_index: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    docking_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED, nullable=False, index=True)
    assigned_gpu: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    completed_ns: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    ns_per_day: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    current_step: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class GpuStatus(Base):
    __tablename__ = "gpustatus"

    gpu_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(120), default="GPU", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=GpuStatusEnum.AVAILABLE, nullable=False)
    utilization: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    memory_used: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    memory_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    temperature: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    assigned_subjob_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Pool the GPU is reserved for ("md" | "design" | "excluded"); see GpuPool.
    pool: Mapped[str] = mapped_column(String(16), default=GpuPool.MD, nullable=False, index=True)
    # How many subjobs may run on this GPU at once (parallel MD); 1 = exclusive.
    capacity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Authoritative count of subjobs currently occupying a slot on this GPU (0..capacity).
    running_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class JobLog(Base):
    __tablename__ = "joblogs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    subjob_id: Mapped[str | None] = mapped_column(String(80), index=True, nullable=True)
    level: Mapped[str] = mapped_column(String(10), default=LogLevel.INFO, nullable=False)
    step: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ResourceUsage(Base):
    __tablename__ = "resourceusage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subjob_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    cpu_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    memory_used: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    disk_used: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class DesignJob(Base):
    """A peptide-design GA run: evolve peptides to bind a fixed target compound.

    Reuses the JobStatus enum for ``status`` (queued/preparing/running_md/analyzing/
    completed/failed/cancelled). Runs on the GPU design pool; docking is CPU-only, MD
    evaluation of the per-generation elites uses the design-pool GPU(s).
    """

    __tablename__ = "designjobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED, nullable=False, index=True)

    # Target compound (the ligand the peptides must bind).
    compound_name: Mapped[str] = mapped_column(String(255), default="compound", nullable=False)
    compound_file: Mapped[str] = mapped_column(String(512), nullable=False)  # path to sdf/mol/smiles

    # GA configuration.
    initial_sequences: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list[str]
    peptide_length: Mapped[int] = mapped_column(Integer, nullable=False)
    population_size: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    num_generations: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    # top_k_md is retained for back-compat with existing rows but no longer used (superseded by
    # dock_oversample: dock population_size × dock_oversample, then MD the top population_size).
    top_k_md: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    dock_oversample: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    md_length_ns: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    # Independent MD replicas per evaluated candidate; GA fitness uses the replica-mean ΔG.
    n_replicas: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    exhaustiveness: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    # Per-generation evaluation policy: "hybrid" (dock all -> MD top-k) | "md_only" (MD all).
    eval_mode: Mapped[str] = mapped_column(String(16), default="hybrid", nullable=False)
    # Docking engine for this run: "vina" (default) | "smina" | "auto".
    dock_engine: Mapped[str] = mapped_column(String(16), default="vina", nullable=False)
    # Design strategy: "ga" (genetic algorithm, default) | "autoscientist" (LLM self-organizing
    # agent team, arXiv:2605.28655). Numeric knobs (population_size/num_generations/dock_oversample)
    # are reinterpreted per strategy — see worker/mdworker/design/autoscientist.py.
    strategy: Mapped[str] = mapped_column(String(20), default="ga", nullable=False, index=True)

    # Progress + results.
    current_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    assigned_gpu: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_sequence: Mapped[str | None] = mapped_column(String(256), nullable=True)
    best_fitness: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_docking_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_md_dg: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class DesignCandidate(Base):
    """One peptide evaluated during a design run (a GA individual in some generation)."""

    __tablename__ = "designcandidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    design_job_id: Mapped[str] = mapped_column(String(64), ForeignKey("designjobs.id"), nullable=False, index=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    sequence: Mapped[str] = mapped_column(String(256), nullable=False)
    docking_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    md_dg: Mapped[float | None] = mapped_column(Float, nullable=True)
    fitness: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    refined: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # MD-evaluated
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AppSetting(Base):
    """Admin-editable runtime key/value settings (e.g. the Gemini report API key + model).

    A DB value set from the Admin tab takes precedence over the corresponding environment
    default, so an operator can configure/rotate the key without redeploying. Created
    automatically by create_all; admin-only at the API layer."""

    __tablename__ = "appsettings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
