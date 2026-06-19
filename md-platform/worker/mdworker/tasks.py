"""RQ task entry points (CONTRACT §5 enqueue contract).

In `rq` queue mode the backend enqueues ``run_subjob_task(subjob_id, settings_payload)`` for MD
and ``run_design_task(design_id, config, settings_payload)`` for design. Each task builds an HTTP
reporter from the environment (BACKEND_URL + INTERNAL_API_TOKEN). In `local` queue mode the backend
calls the runners directly with DB reporters, so this module is only used by containerized workers.
"""

from __future__ import annotations

import sys
from typing import Any, Dict

from mdworker.config import load_settings
from mdworker.design.reporter import HttpDesignReporter
from mdworker.pipeline.context import HttpReporter
from mdworker.pipeline import runner


def _design_settings_payload(settings) -> dict:
    return {
        "GEMINI_API_KEY": settings.gemini_api_key,
        "GEMINI_MODEL": settings.gemini_model,
        "REPORT_ENABLED": settings.report_enabled,
        "STORAGE_ROOT": settings.storage_root,
        "MD_ENGINE": settings.resolved_engine,
        "MDP_TEMPLATE_DIR": settings.mdp_template_dir,
        "TRAJECTORY_OUTPUT_PS": settings.trajectory_output_ps,
        "PROTEIN_FORCE_FIELD": settings.protein_force_field,
        "LIGAND_FORCE_FIELD": settings.ligand_force_field,
        "LIGAND_CHARGE_METHOD": settings.ligand_charge_method,
        "WATER_MODEL": settings.water_model,
        "PROTEIN_FORCE_FIELD_FALLBACK": settings.protein_force_field_fallback,
        "WATER_MODEL_FALLBACK": settings.water_model_fallback,
        "FORCEFIELD_AUTOFALLBACK": settings.forcefield_autofallback,
        "BOX_PADDING_NM": settings.box_padding_nm,
        "NVT_STEPS": settings.nvt_steps,
        "NPT_STEPS": settings.npt_steps,
    }


def run_subjob_task(subjob_id: str, settings_payload: dict | None = None) -> Dict[str, Any]:
    """RQ job function: run one subjob, reporting to the backend over HTTP."""
    settings = load_settings()
    reporter = HttpReporter(settings.backend_url, settings.internal_api_token)
    try:
        return runner.run_subjob(subjob_id, reporter=reporter, settings=settings_payload or settings)
    finally:
        reporter.close()


def run_design_task(design_id: str, config: dict, settings_payload: dict | None = None) -> Dict[str, Any]:
    """RQ job function: run one peptide-design job in the worker image."""
    settings = load_settings()
    run_settings = settings_payload or _design_settings_payload(settings)
    reporter = HttpDesignReporter(settings.backend_url, settings.internal_api_token)
    try:
        strategy = str(config.get("strategy", "ga")).lower()
        if strategy == "autoscientist":
            from mdworker.design.autoscientist import run_autoscientist as _run
        else:
            from mdworker.design.runner import run_design as _run
        return _run(design_id, config, reporter, run_settings)
    except Exception as exc:  # noqa: BLE001 - keep the backend row terminal if the RQ task fails
        reporter.set_status(design_id, "failed", error_message=str(exc))
        reporter.log(design_id, "error", "design", f"Design execution error: {exc}")
        raise
    finally:
        reporter.close()


def _cli_run_subjob(argv=None) -> int:
    """Console-script entry (`mdworker-run <subjob_id>`): run a subjob from the CLI.

    Useful for manual re-runs / debugging on a worker host.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: mdworker-run <subjob_id>", file=sys.stderr)
        return 2
    result = run_subjob_task(argv[0])
    print(result)
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(_cli_run_subjob())
