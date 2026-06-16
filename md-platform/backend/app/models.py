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
