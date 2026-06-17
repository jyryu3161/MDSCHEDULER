"""Tier 2 (worker side) — MD replica id parsing + per-replica working directory.

Replica 1 must be byte-identical to the pre-replica behavior (`{job}_pose_NN` / `pose_NN`);
replicas 2..N add a `_rep_RR` suffix so independent trajectories never share a directory."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mdworker.pipeline.context import JobContext
from mdworker.pipeline.runner import _parse_subjob_id


class _Reporter:
    def update_subjob(self, *a, **k): ...
    def log(self, *a, **k): ...


# ── _parse_subjob_id ─────────────────────────────────────────────────────────
def test_parse_single_replica_id_is_backward_compatible():
    assert _parse_subjob_id("md0001_pose_02") == ("md0001", 2, 1)


def test_parse_replica_id():
    assert _parse_subjob_id("md0001_pose_02_rep_03") == ("md0001", 2, 3)


def test_parse_job_id_with_underscores():
    assert _parse_subjob_id("job_abc_123_pose_05_rep_02") == ("job_abc_123", 5, 2)


def test_parse_invalid_raises():
    with pytest.raises(ValueError):
        _parse_subjob_id("not-a-subjob-id")


# ── JobContext per-replica directory ─────────────────────────────────────────
def _ctx(tmp_path, pose, replica):
    return JobContext(
        job_id="md0001", subjob_id=f"md0001_pose_{pose:02d}", pose_index=pose,
        storage_root=str(tmp_path), reporter=_Reporter(), job_meta={}, subjob_meta={},
        replica_index=replica,
    )


def test_context_replica1_uses_plain_pose_dir(tmp_path):
    ctx = _ctx(tmp_path, 1, 1)
    assert ctx.pose_name == "pose_01"
    assert ctx.pose_dir.name == "pose_01"


def test_context_replica_n_uses_suffixed_dir(tmp_path):
    ctx = _ctx(tmp_path, 1, 3)
    assert ctx.pose_name == "pose_01_rep_03"
    assert ctx.md_dir == tmp_path / "jobs" / "md0001" / "pose_01_rep_03" / "md"


def test_replica_dirs_do_not_collide(tmp_path):
    a = _ctx(tmp_path, 2, 1).pose_dir
    b = _ctx(tmp_path, 2, 2).pose_dir
    assert a != b


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
