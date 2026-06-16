"""Validation service (CONTRACT §7).

Delegates the heavy lifting (PDBQT pose parsing, chemistry/atom-mapping) to the
worker's ``validate_input`` so there is exactly one implementation of the rules.
The worker is import-guarded: if ``mdworker`` (or its rdkit dependency) is absent,
validation returns a clear, actionable error instead of crashing the backend.

Hard rules enforced here (CONTRACT §7) at job-creation time:
  - CHEMISTRY_REQUIRED : raw PDBQT only + REQUIRE_LIGAND_CHEMISTRY
  - ATOM_MAPPING_FAILED : atom_mapping.success == false
  - CHEMISTRY_MISMATCH : heavy-atom formula/count mismatch
  - SMILES path requires ALLOW_SMILES_INPUT + successful mapping
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..schemas import ValidationReport

# Sentinel embedded as the first error string when the validator could not run,
# so enforce_hard_rules can map it to CODE_VALIDATION_UNAVAILABLE deterministically.
_UNAVAILABLE_MARKER = "[VALIDATION_UNAVAILABLE] "

# Codes (CONTRACT §7).
CODE_CHEMISTRY_REQUIRED = "CHEMISTRY_REQUIRED"
CODE_ATOM_MAPPING_FAILED = "ATOM_MAPPING_FAILED"
CODE_CHEMISTRY_MISMATCH = "CHEMISTRY_MISMATCH"
CODE_SMILES_NOT_ALLOWED = "SMILES_NOT_ALLOWED"
CODE_MEEKO_NOT_ALLOWED = "MEEKO_NOT_ALLOWED"
CODE_VALIDATION_UNAVAILABLE = "VALIDATION_UNAVAILABLE"
CODE_NO_POSES = "NO_POSES"
CODE_INVALID_UPLOAD = "INVALID_UPLOAD"


class WorkerUnavailableError(RuntimeError):
    """Raised when the mdworker validator cannot be imported/used."""


def _import_validator():
    """Import ``mdworker.pipeline.steps.validate_input.validate_input``.

    Falls back to the package-level ``validate_input`` if exposed. Raises
    WorkerUnavailableError with an actionable message on any failure.
    """
    try:
        # Preferred location per CONTRACT §9 step 1.
        from mdworker.pipeline.steps.validate_input import validate_input  # type: ignore

        return validate_input
    except Exception:
        pass
    try:
        from mdworker import validate_input  # type: ignore

        return validate_input
    except Exception as exc:  # noqa: BLE001
        raise WorkerUnavailableError(
            "The 'mdworker' validation component is not installed or failed to import "
            f"({exc.__class__.__name__}: {exc}). Install it with 'pip install -e ./worker' "
            "and ensure rdkit is available."
        ) from exc


def run_validation(
    *,
    pose_file: Path | None,
    chemistry_file: Path | None,
    receptor_file: Path | None,
    smiles: str | None,
    settings: Settings | None = None,
) -> ValidationReport:
    """Build a ValidationReport via the worker validator.

    The worker returns a plain dict matching CONTRACT §7; we coerce it into the
    typed ValidationReport. If the worker is unavailable, a report with ok=false
    and a VALIDATION_UNAVAILABLE error is returned (so the upload step degrades
    gracefully and the UI can show a clear message).
    """
    settings = settings or get_settings()
    try:
        validate_input = _import_validator()
    except WorkerUnavailableError as exc:
        # Machine-readable marker so enforce_hard_rules returns VALIDATION_UNAVAILABLE.
        return ValidationReport(
            ok=False,
            input_type="pdbqt",
            errors=[_UNAVAILABLE_MARKER + str(exc)],
        )

    # Build the kwargs the worker accepts by inspecting its signature, rather than
    # catching TypeError (which would also swallow genuine bugs inside the worker).
    pose = str(pose_file) if pose_file else None
    chem = str(chemistry_file) if chemistry_file else None
    rec = str(receptor_file) if receptor_file else None
    smi = smiles or None
    settings_payload = {
        "REQUIRE_LIGAND_CHEMISTRY": settings.REQUIRE_LIGAND_CHEMISTRY,
        "ALLOW_SMILES_INPUT": settings.ALLOW_SMILES_INPUT,
        "ALLOW_MEEKO_MAPPING_INPUT": settings.ALLOW_MEEKO_MAPPING_INPUT,
    }

    # Value to use when a worker exposes a parameter under one of these names. We
    # support both a bundled ``settings`` dict and the individual flag parameters the
    # real worker (CONTRACT §9 step 1) exposes, so the correct configuration reaches
    # the validator regardless of which calling convention the worker chose.
    value_by_name = {
        "pose_file": pose,
        "pose": pose,
        "chemistry_file": chem,
        "chem": chem,
        "chemistry": chem,
        "receptor_file": rec,
        "receptor": rec,
        "smiles": smi,
        "settings": settings_payload,
        "require_ligand_chemistry": settings.REQUIRE_LIGAND_CHEMISTRY,
        "allow_smiles_input": settings.ALLOW_SMILES_INPUT,
        "allow_meeko_mapping_input": settings.ALLOW_MEEKO_MAPPING_INPUT,
    }

    call_args: list[Any] = []
    call_kwargs: dict[str, Any] = {}
    introspected = True
    try:
        sig = inspect.signature(validate_input)
    except (TypeError, ValueError):
        introspected = False

    if introspected:
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        for name, param in sig.parameters.items():
            if param.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
                continue
            if name not in value_by_name:
                continue
            if param.kind == inspect.Parameter.POSITIONAL_ONLY:
                call_args.append(value_by_name[name])
            else:
                call_kwargs[name] = value_by_name[name]
        # If the worker takes **kwargs, also pass the full keyword set it didn't name explicitly.
        if accepts_var_kw:
            for name in ("pose_file", "chemistry_file", "receptor_file", "smiles", "settings"):
                call_kwargs.setdefault(name, value_by_name[name])
    else:
        # Non-introspectable (C/builtin) callable: positional order from CONTRACT §9.
        call_args = [pose, chem, rec, smi]

    try:
        raw: dict[str, Any] = validate_input(*call_args, **call_kwargs)
    except Exception as exc:  # noqa: BLE001
        return ValidationReport(
            ok=False,
            input_type="pdbqt",
            errors=[f"Validation failed: {exc.__class__.__name__}: {exc}"],
        )

    if isinstance(raw, ValidationReport):
        return raw
    return ValidationReport.model_validate(raw)


def enforce_hard_rules(
    report: ValidationReport,
    *,
    settings: Settings | None = None,
) -> tuple[bool, str | None, str | None]:
    """Apply CONTRACT §7 hard rules to a report.

    Returns (ok, code, message). ok=True means job creation may proceed.
    """
    settings = settings or get_settings()

    # A validator-unavailable marker always wins (the report carries no usable data).
    if report.errors and report.errors[0].startswith(_UNAVAILABLE_MARKER):
        first = report.errors[0]
        return (False, CODE_VALIDATION_UNAVAILABLE, first[len(_UNAVAILABLE_MARKER):])

    rep_source = (report.chem_source or "none").lower()
    has_chemistry = rep_source in ("sdf", "mol2", "smiles", "meeko")

    # Contract-specific hard rules are evaluated FIRST so their precise codes
    # (CHEMISTRY_REQUIRED / ATOM_MAPPING_FAILED / CHEMISTRY_MISMATCH) take precedence
    # over any free-text message the worker may have additionally placed in errors.

    # Rule: raw PDBQT only (no chemistry) when chemistry is required.
    if settings.REQUIRE_LIGAND_CHEMISTRY and not has_chemistry:
        return (
            False,
            CODE_CHEMISTRY_REQUIRED,
            "Ligand chemistry is required: provide an SDF/MOL2 template, a valid SMILES, "
            "or a Meeko mapping. Bond orders cannot be perceived from PDBQT alone.",
        )

    # Rule: SMILES path requires ALLOW_SMILES_INPUT.
    if rep_source == "smiles" and not settings.ALLOW_SMILES_INPUT:
        return (False, CODE_SMILES_NOT_ALLOWED, "SMILES input is disabled by configuration.")

    # Rule: Meeko mapping requires ALLOW_MEEKO_MAPPING_INPUT.
    if rep_source == "meeko" and not settings.ALLOW_MEEKO_MAPPING_INPUT:
        return (False, CODE_MEEKO_NOT_ALLOWED, "Meeko mapping input is disabled by configuration.")

    # Rule: atom mapping must succeed when chemistry is present.
    mapping = report.atom_mapping
    if has_chemistry and mapping.attempted and not mapping.success:
        # Distinguish a pure formula/count mismatch from a generic mapping failure.
        tmpl = mapping.template_heavy_atoms
        pose = mapping.pose_heavy_atoms
        formula_tmpl = mapping.molformula_template
        formula_pose = mapping.molformula_pose
        is_mismatch = (
            (tmpl is not None and pose is not None and tmpl != pose)
            or (formula_tmpl and formula_pose and formula_tmpl != formula_pose)
        )
        if is_mismatch:
            return (
                False,
                CODE_CHEMISTRY_MISMATCH,
                mapping.message
                or "Heavy-atom composition mismatch between the chemistry template and the docked poses.",
            )
        return (
            False,
            CODE_ATOM_MAPPING_FAILED,
            mapping.message or "Failed to map bond orders from the chemistry template onto the poses.",
        )

    # Rule: no poses parsed.
    if report.pose_count <= 0:
        return (False, CODE_NO_POSES, "No docking poses were detected in the PDBQT file.")

    # Fallback: the worker reported a problem that matched none of the specific hard
    # rules above (e.g. an unreadable receptor). Reject with a generic code so the
    # client still sees an actionable message and the report.
    if report.errors:
        return (False, CODE_INVALID_UPLOAD, report.errors[0])
    if not report.ok:
        return (False, CODE_INVALID_UPLOAD, "Input validation did not pass.")

    return (True, None, None)
