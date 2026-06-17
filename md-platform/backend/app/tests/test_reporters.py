"""M4 — terminal-resurrection guard in the local-executor reporters.

Once a subjob (or design job) is terminal — e.g. cancelled by the user — a late status report
from a worker that hadn't observed the cancel must NOT flip it back to running/completed.
Telemetry fields are still applied so late metrics aren't lost. Mirrors the HTTP internal path."""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="mdplatform_reporters_")
os.environ.setdefault("STORAGE_ROOT", _TMP)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/test.db")
os.environ.setdefault("QUEUE_BACKEND", "local")
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import DesignJob, Job, JobStatus, SubJob, utcnow  # noqa: E402
from app.services.db_reporter import DbReporter  # noqa: E402
from app.services.design_reporter import DesignReporter  # noqa: E402

# Safety guard (same as test_gpu_pools): only ever drop_all/create_all against a throwaway SQLite
# DB under the OS temp dir, never a real/local database, even if DATABASE_URL was pre-set.
_TMPDIR = os.path.realpath(tempfile.gettempdir())
_URL = str(engine.url)
_db_path = os.path.realpath(_URL.split("sqlite:///", 1)[-1])
# Explicit raise (not assert) so the destructive-DB guard survives python -O / PYTHONOPTIMIZE.
if not (_URL.startswith("sqlite") and _db_path.startswith(_TMPDIR + os.sep)):
    raise RuntimeError(f"refusing destructive setup against non-temp DB: {_URL}")


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _make_job_with_subjob(status=JobStatus.CANCELLED):
    db = SessionLocal()
    try:
        db.add(Job(id="j1", user_id=1, name="t", input_type="pdb", ligand_type="peptide",
                   status=status, md_length_ns=10, top_n_poses=1, force_field="ff19SB",
                   ligand_force_field="gaff2", ligand_chem_source="sdf", water_model="opc",
                   salt_concentration=0.15, temperature=300, pressure=1.0, box_type="cubic",
                   priority="normal", created_at=utcnow()))
        db.add(SubJob(id="j1_pose_01", job_id="j1", pose_index=1, replica_index=1,
                      docking_score=-5.0, status=status, progress=42.0, completed_ns=4.0,
                      ns_per_day=0.0, current_step="cancelled"))
        db.commit()
    finally:
        db.close()


def test_cancelled_subjob_not_resurrected_by_late_completed():
    _make_job_with_subjob(JobStatus.CANCELLED)
    reporter = DbReporter(SessionLocal)
    # A worker that finished after the cancel reports completed + telemetry.
    reporter.set_subjob_status("j1_pose_01", status=JobStatus.COMPLETED,
                               progress=100.0, completed_ns=10.0, ns_per_day=5.0)
    db = SessionLocal()
    try:
        sub = db.get(SubJob, "j1_pose_01")
        assert sub.status == JobStatus.CANCELLED          # status NOT resurrected
        assert sub.completed_ns == 10.0 and sub.ns_per_day == 5.0  # telemetry still applied
    finally:
        db.close()


def test_running_subjob_status_still_updates_normally():
    _make_job_with_subjob(JobStatus.RUNNING_MD)
    reporter = DbReporter(SessionLocal)
    reporter.set_subjob_status("j1_pose_01", status=JobStatus.COMPLETED, progress=100.0)
    db = SessionLocal()
    try:
        assert db.get(SubJob, "j1_pose_01").status == JobStatus.COMPLETED  # non-terminal -> applied
    finally:
        db.close()


def _make_design(status=JobStatus.CANCELLED):
    db = SessionLocal()
    try:
        db.add(DesignJob(id="d1", user_id=1, name="d", status=status, compound_name="c",
                         compound_file="/tmp/c.smi", initial_sequences='["KCCIVYP"]',
                         peptide_length=7, population_size=4, num_generations=2, top_k_md=1,
                         md_length_ns=5, current_generation=0, progress=10.0, created_at=utcnow()))
        db.commit()
    finally:
        db.close()


def test_cancelled_design_not_resurrected():
    _make_design(JobStatus.CANCELLED)
    reporter = DesignReporter(SessionLocal)
    reporter.set_status("d1", JobStatus.COMPLETED, current_generation=2)
    db = SessionLocal()
    try:
        dj = db.get(DesignJob, "d1")
        assert dj.status == JobStatus.CANCELLED       # not resurrected
        assert dj.current_generation == 2             # telemetry still applied
        assert dj.progress == 10.0                    # terminal side-effect (progress=100) NOT applied
    finally:
        db.close()


def test_running_design_status_updates_normally():
    _make_design(JobStatus.RUNNING_MD)
    reporter = DesignReporter(SessionLocal)
    reporter.set_status("d1", JobStatus.COMPLETED)
    db = SessionLocal()
    try:
        dj = db.get(DesignJob, "d1")
        assert dj.status == JobStatus.COMPLETED and dj.progress == 100.0
    finally:
        db.close()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
