"""mdworker — MD Platform docking-to-MD worker package.

Public surface:
    - mdworker.config.Settings / load_settings()
    - mdworker.pipeline.context.Reporter (Protocol), HttpReporter, JobContext
    - mdworker.pipeline.runner.run_subjob(subjob_id, *, reporter, settings)
    - mdworker.pipeline.steps.validate_input.validate_input(...)  (import-light; reused by backend)
    - mdworker.tasks.run_subjob_task(subjob_id)

The package is installable (see worker/pyproject.toml, project name "mdworker") and is
imported as a normal module by the backend's LocalExecutor for the Reporter Protocol and
the import-light validate_input step.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
