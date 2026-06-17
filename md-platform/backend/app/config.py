"""Application configuration.

Reads every environment variable defined in CONTRACT §1 via pydantic-settings,
with the defaults shown there. For local dev without Docker the contract
recommends:  STORAGE_ROOT=./storage, DATABASE_URL=sqlite:///./storage/md_platform.db,
QUEUE_BACKEND=local, MD_ENGINE=mock.
"""

from __future__ import annotations

import shutil
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _gmx_available() -> bool:
    """True if a GROMACS `gmx` binary is on PATH."""
    return shutil.which("gmx") is not None


def _redis_reachable(redis_url: str) -> bool:
    """Best-effort ping of REDIS_URL. Returns False on any error."""
    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(redis_url, socket_connect_timeout=0.5, socket_timeout=0.5)
        return bool(client.ping())
    except Exception:
        return False


def _detect_num_gpus() -> int:
    """Detect GPU count via nvidia-smi; 0 if unavailable."""
    import subprocess

    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return 0
        return len([ln for ln in out.stdout.splitlines() if ln.strip()])
    except Exception:
        return 0


class Settings(BaseSettings):
    """Typed view of the runtime environment (CONTRACT §1)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    APP_PORT: int = 8888
    DEFAULT_ADMIN_ID: str = "csbl"
    DEFAULT_ADMIN_PASSWORD: str = "csbl"
    DEFAULT_MD_LENGTH_NS: int = 50
    DEFAULT_TOP_N_POSES: int = 3

    STORAGE_ROOT: str = "/app/storage"
    DATABASE_URL: str = "sqlite:////app/storage/md_platform.db"
    REDIS_URL: str = "redis://redis:6379/0"

    MAX_UPLOAD_SIZE_GB: int = 10
    GPU_ASSIGNMENT_MODE: str = "one_job_per_gpu"

    MD_ENGINE: str = "auto"  # gromacs | mock | auto
    DOCK_ENGINE: str = "vina"  # peptide-design docking: vina (default, AutoDock Vina 1.2.7, rigid) | smina (flexible side chains) | auto
    # Default to ff19SB + OPC (recommended for binding studies). The value MUST be the GROMACS
    # force-field PORT DIRECTORY name (`gmx pdb2gmx -ff`); the ff19SB port ships as "amber19sb.ff",
    # so the default is "amber19sb" (the literal "ff19SB" would not match and would silently fall
    # back). The worker pre-flights the GROMACS install and falls back to the *_FALLBACK pair when
    # the port is absent.
    PROTEIN_FORCE_FIELD: str = "amber19sb"
    LIGAND_FORCE_FIELD: str = "gaff2"
    LIGAND_CHARGE_METHOD: str = "am1bcc"
    WATER_MODEL: str = "opc"
    PROTEIN_FORCE_FIELD_FALLBACK: str = "amber14sb"
    WATER_MODEL_FALLBACK: str = "tip3p"
    FORCEFIELD_AUTOFALLBACK: bool = True
    # Solvation/equilibration protocol (forwarded to the worker).
    BOX_PADDING_NM: float = 1.2
    NVT_STEPS: int = 50000
    NPT_STEPS: int = 125000

    REQUIRE_LIGAND_CHEMISTRY: bool = True
    ALLOW_SMILES_INPUT: bool = True
    ALLOW_MEEKO_MAPPING_INPUT: bool = True

    JWT_SECRET: str = "change-me-in-production"
    JWT_EXPIRE_MINUTES: int = 480

    QUEUE_BACKEND: str = "auto"  # rq | local | auto
    INTERNAL_API_TOKEN: str = "internal-worker-token-change-me"

    NUM_GPUS: str = "auto"  # auto | integer-as-string

    # GPU pool partitioning (CONTRACT §2 gpustatus.pool). MD jobs run on the MD pool; the
    # peptide-design subsystem runs on the design pool, so the two never contend for a device.
    # Comma-separated GPU ids; empty MD_GPU_IDS => "all GPUs not in the design pool".
    MD_GPU_IDS: str = ""
    DESIGN_GPU_IDS: str = ""
    # How many MD subjobs may share one MD-pool GPU (parallel MD). Default 1 = current
    # behavior; adjustable at runtime from the dashboard (persisted to gpustatus.capacity).
    MD_GPU_CONCURRENCY: int = 1

    MD_MOCK_SPEEDUP: int = 2000
    TRAJECTORY_OUTPUT_PS: int = 100
    RETENTION_DAYS: int = 30

    # Worker seam (CONTRACT §5). The backend hands this to the worker so the
    # HttpReporter knows where to post; for in-process LocalExecutor it is unused.
    BACKEND_URL: str = "http://backend:8000"
    MDP_TEMPLATE_DIR: str = "/app/md-env/templates/gromacs"

    @field_validator("REQUIRE_LIGAND_CHEMISTRY", "ALLOW_SMILES_INPUT", "ALLOW_MEEKO_MAPPING_INPUT", mode="before")
    @classmethod
    def _coerce_bool(cls, v):  # noqa: ANN001
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "on"}
        return bool(v)

    # ---- Derived helpers -------------------------------------------------

    @property
    def storage_root(self) -> Path:
        return Path(self.STORAGE_ROOT).resolve()

    @property
    def max_upload_bytes(self) -> int:
        return int(self.MAX_UPLOAD_SIZE_GB) * 1024 * 1024 * 1024

    def resolved_md_engine(self) -> str:
        """Resolve MD_ENGINE=auto -> gromacs if `gmx` on PATH else mock."""
        engine = (self.MD_ENGINE or "auto").lower()
        if engine == "auto":
            return "gromacs" if _gmx_available() else "mock"
        return engine

    def resolved_queue_backend(self) -> str:
        """Resolve QUEUE_BACKEND=auto -> rq if redis reachable else local."""
        backend = (self.QUEUE_BACKEND or "auto").lower()
        if backend == "auto":
            return "rq" if _redis_reachable(self.REDIS_URL) else "local"
        return backend

    def resolved_num_gpus(self) -> int:
        """Resolve NUM_GPUS=auto via nvidia-smi; integer string to force.

        Falls back to 1 placeholder when auto-detection finds nothing, so the
        local executor always has at least one worker slot.
        """
        val = str(self.NUM_GPUS).strip().lower()
        if val == "auto":
            detected = _detect_num_gpus()
            return detected if detected > 0 else 1
        try:
            n = int(val)
            return max(n, 0)
        except ValueError:
            return 1


    @staticmethod
    def _parse_ids(spec: str, valid: set[int]) -> list[int]:
        out: list[int] = []
        for tok in str(spec or "").replace(";", ",").split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                gid = int(tok)
            except ValueError:
                continue
            if gid in valid and gid not in out:
                out.append(gid)
        return out

    def resolved_gpu_pools(self) -> dict[int, str]:
        """Map each GPU id -> pool ("md" | "design" | "excluded").

        DESIGN_GPU_IDS takes precedence on overlap. When MD_GPU_IDS is set, only those ids form
        the MD pool and any GPU in neither list is "excluded" (unmanaged — left free for other
        lab use). When MD_GPU_IDS is empty, every non-design GPU is MD. Ids outside the detected
        device range are dropped (validation) rather than silently scheduled.
        """
        valid = set(range(self.resolved_num_gpus()))
        design = set(self._parse_ids(self.DESIGN_GPU_IDS, valid))
        md_spec = self._parse_ids(self.MD_GPU_IDS, valid)
        if self.MD_GPU_IDS.strip():
            md = set(md_spec) - design          # explicit MD pool; unlisted GPUs are excluded
        else:
            md = valid - design                 # default: claim every non-design GPU for MD

        def _pool(gid: int) -> str:
            if gid in design:
                return "design"
            if gid in md:
                return "md"
            return "excluded"

        return {gid: _pool(gid) for gid in valid}

    def resolved_md_concurrency(self) -> int:
        try:
            return max(1, int(self.MD_GPU_CONCURRENCY))
        except (TypeError, ValueError):
            return 1


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
