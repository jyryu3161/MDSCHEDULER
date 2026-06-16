"""MD engine selection (CONTRACT §1 MD_ENGINE, §9).

get_engine(settings) returns a GromacsEngine when `gmx` is on PATH (or MD_ENGINE=gromacs),
otherwise a MockEngine. Both engines implement the same interface (see base.MDEngine) so the
pipeline steps (prepare_structure, parameterize_ligand, run_md, analyze_md) are
engine-agnostic.
"""

from __future__ import annotations

import shutil

from .base import MDEngine


def get_engine(settings) -> MDEngine:
    """Return the MD engine implementation for the resolved engine name."""
    name = (settings.md_engine or "auto").strip().lower()
    if name == "gromacs":
        from .gromacs import GromacsEngine

        return GromacsEngine(settings)
    if name == "mock":
        from .mock import MockEngine

        return MockEngine(settings)
    # auto
    if shutil.which("gmx"):
        from .gromacs import GromacsEngine

        return GromacsEngine(settings)
    from .mock import MockEngine

    return MockEngine(settings)


__all__ = ["get_engine", "MDEngine"]
