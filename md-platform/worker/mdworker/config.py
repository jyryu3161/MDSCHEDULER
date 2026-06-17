"""Worker configuration.

All environment variable names match CONTRACT.md §1 exactly. The worker does not depend
on pydantic; it reads os.environ directly so it can be imported by tools (and the backend's
validate path) with no heavy dependencies.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field, asdict
from typing import Optional


def _env(name: str, default: str) -> str:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    """Resolved worker settings (CONTRACT §1)."""

    # Engine selection: gromacs | mock | auto
    md_engine: str = "auto"

    # Backend internal API
    backend_url: str = "http://backend:8000"
    internal_api_token: str = "internal-worker-token-change-me"

    # Force-field / MD chemistry settings.
    # Default to the modern protein FF + 4-point water recommended for binding studies
    # (ff19SB is parameterized against OPC). These require the ff19SB GROMACS port to be
    # installed; when it is absent the engine PRE-FLIGHTS the GROMACS top dirs and falls back
    # to the stock amber14sb + tip3p pair (see *_fallback below) with a logged warning, so the
    # platform still runs on a plain GROMACS install. Override via PROTEIN_FORCE_FIELD /
    # WATER_MODEL.
    protein_force_field: str = "ff19SB"
    ligand_force_field: str = "gaff2"
    ligand_charge_method: str = "am1bcc"
    water_model: str = "opc"
    # Fallback pair used when the requested protein_force_field/water_model is not found in the
    # GROMACS installation. amber14sb + tip3p ship with stock GROMACS.
    protein_force_field_fallback: str = "amber14sb"
    water_model_fallback: str = "tip3p"
    # When True, silently fall back to the *_fallback pair if the requested FF/water is missing.
    # When False, use the requested pair as-is and let gmx pdb2gmx fail loudly (strict mode).
    forcefield_autofallback: bool = True
    require_ligand_chemistry: bool = True
    allow_smiles_input: bool = True
    allow_meeko_mapping_input: bool = True

    # Solvation box padding (gmx editconf -d, nm). 1.2 nm keeps the solute well clear of its
    # periodic image for binding studies (the previous 1.0 nm is on the small side).
    box_padding_nm: float = 1.2
    # Equilibration lengths (steps at dt=0.002 ps). NVT 100 ps + NPT 250 ps (was 100 ps) gives
    # the solvent/box more time to settle around the docked complex before production.
    nvt_steps: int = 50000     # 100 ps
    npt_steps: int = 125000    # 250 ps

    # MDP template directory for the real GROMACS engine
    mdp_template_dir: str = "/app/md-env/templates/gromacs"

    # Storage layout root (CONTRACT §8)
    storage_root: str = "/app/storage"

    # Trajectory / mock tuning
    trajectory_output_ps: int = 100
    md_mock_speedup: int = 2000  # ns of "simulation" per real second

    # Redis (rq worker bootstrap)
    redis_url: str = "redis://redis:6379/0"

    # GPU id this worker is pinned to (compose sets WORKER_GPU_ID); informational only,
    # the actual lock is allocated by the backend via the Reporter.
    worker_gpu_id: Optional[int] = None

    extra: dict = field(default_factory=dict)

    @property
    def resolved_engine(self) -> str:
        """Resolve 'auto' to 'gromacs' if `gmx` on PATH else 'mock'."""
        engine = (self.md_engine or "auto").strip().lower()
        if engine in ("gromacs", "mock"):
            return engine
        # auto
        return "gromacs" if shutil.which("gmx") else "mock"

    def to_dict(self) -> dict:
        return asdict(self)


def load_settings() -> Settings:
    """Build Settings from the process environment (CONTRACT §1 names)."""
    gpu_raw = os.environ.get("WORKER_GPU_ID")
    worker_gpu_id: Optional[int] = None
    if gpu_raw not in (None, ""):
        try:
            worker_gpu_id = int(gpu_raw)
        except ValueError:
            worker_gpu_id = None

    return Settings(
        md_engine=_env("MD_ENGINE", "auto"),
        backend_url=_env("BACKEND_URL", "http://backend:8000").rstrip("/"),
        internal_api_token=_env("INTERNAL_API_TOKEN", "internal-worker-token-change-me"),
        protein_force_field=_env("PROTEIN_FORCE_FIELD", "ff19SB"),
        ligand_force_field=_env("LIGAND_FORCE_FIELD", "gaff2"),
        ligand_charge_method=_env("LIGAND_CHARGE_METHOD", "am1bcc"),
        water_model=_env("WATER_MODEL", "opc"),
        protein_force_field_fallback=_env("PROTEIN_FORCE_FIELD_FALLBACK", "amber14sb"),
        water_model_fallback=_env("WATER_MODEL_FALLBACK", "tip3p"),
        forcefield_autofallback=_env_bool("FORCEFIELD_AUTOFALLBACK", True),
        require_ligand_chemistry=_env_bool("REQUIRE_LIGAND_CHEMISTRY", True),
        allow_smiles_input=_env_bool("ALLOW_SMILES_INPUT", True),
        allow_meeko_mapping_input=_env_bool("ALLOW_MEEKO_MAPPING_INPUT", True),
        mdp_template_dir=_env("MDP_TEMPLATE_DIR", "/app/md-env/templates/gromacs"),
        storage_root=_env("STORAGE_ROOT", "/app/storage"),
        trajectory_output_ps=_env_int("TRAJECTORY_OUTPUT_PS", 100),
        md_mock_speedup=_env_int("MD_MOCK_SPEEDUP", 2000),
        box_padding_nm=_env_float("BOX_PADDING_NM", 1.2),
        nvt_steps=_env_int("NVT_STEPS", 50000),
        npt_steps=_env_int("NPT_STEPS", 125000),
        redis_url=_env("REDIS_URL", "redis://redis:6379/0"),
        worker_gpu_id=worker_gpu_id,
    )
