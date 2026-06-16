"""RQ task entry points (CONTRACT §5 enqueue contract).

In `rq` queue mode the backend enqueues ``mdworker.tasks.run_subjob_task(subjob_id)``; this
builds an HttpReporter from the environment (BACKEND_URL + INTERNAL_API_TOKEN) and runs the
pipeline. In `local` queue mode the backend calls ``mdworker.pipeline.runner.run_subjob``
directly with a DbReporter instead, so this module is only used by the containerized workers.
"""

from __future__ import annotations

import sys
from typing import Any, Dict

from mdworker.config import load_settings
from mdworker.pipeline.context import HttpReporter
from mdworker.pipeline import runner


def run_subjob_task(subjob_id: str) -> Dict[str, Any]:
    """RQ job function: run one subjob, reporting to the backend over HTTP."""
    settings = load_settings()
    reporter = HttpReporter(settings.backend_url, settings.internal_api_token)
    try:
        return runner.run_subjob(subjob_id, reporter=reporter, settings=settings)
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
