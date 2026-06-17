"""Tier 2 (backend side) — MD replica fan-out naming + mean ± SEM aggregation.

Covers the pure stats (n samples = replicas, not frames), the replica-aware id/dir naming
(replica 1 stays byte-identical), and pose_replica_aggregates reading per-replica mmpbsa.json."""

from __future__ import annotations

import json
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="mdplatform_replicas_")
os.environ.setdefault("STORAGE_ROOT", _TMP)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/test.db")
os.environ.setdefault("QUEUE_BACKEND", "local")
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest  # noqa: E402

from app.models import Job, SubJob  # noqa: E402
from app.services import jobs_service, storage  # noqa: E402


# ── replica_stats ────────────────────────────────────────────────────────────
def test_replica_stats_empty():
    s = jobs_service.replica_stats([])
    assert s["n"] == 0 and s["mean"] is None and s["sem"] is None


def test_replica_stats_single_has_no_spread():
    s = jobs_service.replica_stats([-12.5])
    assert s["n"] == 1 and s["mean"] == -12.5 and s["sem"] == 0.0 and s["std"] == 0.0


def test_replica_stats_multi_mean_and_sem():
    # values -10, -12, -14 -> mean -12, sample std 2.0, sem 2/sqrt(3) ≈ 1.155
    s = jobs_service.replica_stats([-10.0, -12.0, -14.0])
    assert s["n"] == 3
    assert s["mean"] == -12.0
    assert s["std"] == 2.0
    assert s["sem"] == pytest.approx(1.155, abs=1e-3)
    assert s["min"] == -14.0 and s["max"] == -10.0


def test_replica_stats_ignores_non_numbers():
    s = jobs_service.replica_stats([-10.0, None, "x", -12.0])  # type: ignore[list-item]
    assert s["n"] == 2 and s["mean"] == -11.0


# ── id + dir naming (replica 1 unchanged) ────────────────────────────────────
def test_subjob_id_replica1_is_backward_compatible():
    assert jobs_service.subjob_id("md0001", 2) == "md0001_pose_02"
    assert jobs_service.subjob_id("md0001", 2, 1) == "md0001_pose_02"


def test_subjob_id_replica_n_suffix():
    assert jobs_service.subjob_id("md0001", 2, 3) == "md0001_pose_02_rep_03"


def test_pose_dirname_replica_naming():
    assert storage.pose_dirname(2) == "pose_02"
    assert storage.pose_dirname(2, 1) == "pose_02"
    assert storage.pose_dirname(2, 3) == "pose_02_rep_03"


# ── pose_replica_aggregates ──────────────────────────────────────────────────
def _write_mmpbsa(job_id: str, pose: int, replica: int, gbsa: float, occ: float) -> None:
    d = storage.pose_dir(job_id, pose, replica) / "analysis"
    d.mkdir(parents=True, exist_ok=True)
    (d / "mmpbsa.json").write_text(json.dumps(
        {"gbsa_dg_kcal_mol": gbsa, "pbsa_dg_kcal_mol": gbsa - 1.0, "pose_occupancy": occ}))


def test_single_replica_job_has_no_aggregate():
    job = Job(id="md_single", n_replicas=1)
    sjs = [SubJob(id="md_single_pose_01", job_id="md_single", pose_index=1, replica_index=1,
                  status="completed")]
    assert jobs_service.pose_replica_aggregates(job, sjs) == []


def test_multi_replica_aggregate_mean_sem():
    job_id = "md_multi"
    job = Job(id=job_id, n_replicas=3)
    sjs = []
    for r, g in ((1, -10.0), (2, -12.0), (3, -14.0)):
        sjs.append(SubJob(id=jobs_service.subjob_id(job_id, 1, r), job_id=job_id,
                          pose_index=1, replica_index=r, status="completed"))
        _write_mmpbsa(job_id, 1, r, g, occ=0.9)
    agg = jobs_service.pose_replica_aggregates(job, sjs)
    assert len(agg) == 1
    pose = agg[0]
    assert pose["pose_index"] == 1 and pose["n_replicas"] == 3
    assert pose["gbsa"]["mean"] == -12.0
    assert pose["gbsa"]["sem"] == pytest.approx(1.155, abs=1e-3)
    assert pose["pose_occupancy"]["mean"] == 0.9
    assert [r["replica_index"] for r in pose["replicas"]] == [1, 2, 3]


def test_aggregate_tolerates_missing_replica_results():
    job_id = "md_partial"
    job = Job(id=job_id, n_replicas=2)
    sjs = [
        SubJob(id=jobs_service.subjob_id(job_id, 1, 1), job_id=job_id, pose_index=1,
               replica_index=1, status="completed"),
        SubJob(id=jobs_service.subjob_id(job_id, 1, 2), job_id=job_id, pose_index=1,
               replica_index=2, status="running"),
    ]
    _write_mmpbsa(job_id, 1, 1, -11.0, occ=0.8)  # replica 2 not finished -> no file
    agg = jobs_service.pose_replica_aggregates(job, sjs)
    assert agg[0]["gbsa"]["n"] == 1            # only the completed replica contributes
    assert agg[0]["gbsa"]["mean"] == -11.0
    assert len(agg[0]["replicas"]) == 2        # both replicas listed, one with None values


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
