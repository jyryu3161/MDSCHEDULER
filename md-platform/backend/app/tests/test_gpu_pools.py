"""Unit tests for GPU pools + capacity-based (parallel-MD) scheduling in gpu_manager.

Runs against a dedicated temp SQLite DB with nvidia-smi stubbed out, so the pool layout is
deterministic (3 placeholder GPUs: 0,1 = MD cap 2; 2 = design cap 1) regardless of the host's
real devices. Validates pool partitioning + isolation, capacity-based multi-occupancy,
idempotent claim, atomic release without double-decrement, runtime concurrency change +
reseed persistence, and pool-full handling.
"""

from __future__ import annotations

import os
import tempfile

# Configure a temp SQLite DB BEFORE importing the app. setdefault so that when this module is
# imported alongside the other suites (which share one process + a module-level engine), we
# reuse whatever temp DB was set first; standalone we create our own.
_TMP = tempfile.mkdtemp(prefix="mdplatform_gpupool_")
os.environ.setdefault("STORAGE_ROOT", _TMP)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/test.db")
os.environ.setdefault("QUEUE_BACKEND", "local")
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import GpuPool, Job, JobStatus, SubJob, utcnow  # noqa: E402
from app.services import gpu_manager as G  # noqa: E402

# Safety guard: only ever drop_all/create_all against a throwaway SQLite DB under the OS temp
# dir, and only write files under a temp STORAGE_ROOT — never a real/local database or path.
_TMPDIR = os.path.realpath(tempfile.gettempdir())
_URL = str(engine.url)
_db_path = os.path.realpath(_URL.split("sqlite:///", 1)[-1])
assert _URL.startswith("sqlite") and _db_path.startswith(_TMPDIR + os.sep), \
    f"refusing destructive setup against non-temp DB: {_URL}"
assert os.path.realpath(os.environ.get("STORAGE_ROOT", "")).startswith(_TMPDIR + os.sep), \
    f"refusing to run with non-temp STORAGE_ROOT: {os.environ.get('STORAGE_ROOT')!r}"


class _FakeSettings:
    """Deterministic pool layout independent of the host's GPUs or cached global settings:
    GPUs 0,1 = MD pool (capacity 2); GPU 2 = design pool (capacity 1)."""

    def resolved_num_gpus(self):
        return 3

    def resolved_gpu_pools(self):
        return {0: GpuPool.MD, 1: GpuPool.MD, 2: GpuPool.DESIGN}

    def resolved_md_concurrency(self):
        return 2


@pytest.fixture(autouse=True)
def _deterministic_pools(monkeypatch):
    # No nvidia-smi (placeholder rows) + a fixed pool/concurrency config, so the layout does
    # not depend on the real host or on which test module cached get_settings() first.
    monkeypatch.setattr(G, "query_nvidia_smi", lambda: None)
    monkeypatch.setattr(G, "get_settings", lambda: _FakeSettings())


def _fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    G.seed_gpus(db)
    job = Job(id="j1", user_id=1, name="t", input_type="pdb", ligand_type="peptide",
              status="queued", md_length_ns=10, top_n_poses=9, force_field="amber14sb",
              ligand_force_field="gaff2", ligand_chem_source="sdf", water_model="tip3p",
              salt_concentration=0.15, temperature=300, pressure=1.0, box_type="cubic",
              priority="normal", created_at=utcnow())
    db.add(job)
    for i in range(8):
        db.add(SubJob(id=f"s{i}", job_id="j1", pose_index=i, docking_score=-5.0,
                      status=JobStatus.RUNNING_MD, progress=0, completed_ns=0,
                      ns_per_day=0, current_step="run_md"))
    db.commit()
    return db


def test_pool_partitioning_and_capacity_seed():
    db = _fresh_db()
    rows = {r.gpu_id: r for r in G.list_gpus(db)}
    assert len(rows) == 3
    assert rows[2].pool == GpuPool.DESIGN and rows[2].capacity == 1
    assert rows[0].pool == GpuPool.MD and rows[0].capacity == 2
    assert rows[1].pool == GpuPool.MD and rows[1].capacity == 2


def test_md_claims_never_touch_design_pool():
    db = _fresh_db()
    for s in ["s0", "s1", "s2", "s3"]:
        gid = G.request_gpu(db, s, pool=GpuPool.MD)
        assert gid in (0, 1)                # only MD GPUs, never the design GPU 2
    assert G.request_gpu(db, "s7", pool=GpuPool.DESIGN) == 2   # design claimable independently


def test_capacity_multi_occupancy_then_full():
    db = _fresh_db()
    # 2 MD GPUs x cap 2 = 4 slots; least-loaded spread fills both GPUs to 2 each.
    claims = [G.request_gpu(db, f"s{i}", pool=GpuPool.MD) for i in range(4)]
    assert all(c in (0, 1) for c in claims)
    rows = {r.gpu_id: r for r in G.list_gpus(db)}
    assert rows[0].running_count == 2 and rows[0].status == "busy"   # multi-occupancy
    assert rows[1].running_count == 2 and rows[1].status == "busy"
    assert G.request_gpu(db, "s4", pool=GpuPool.MD) is None          # pool full


def test_idempotent_claim_and_release_no_double_decrement():
    db = _fresh_db()
    gid = G.request_gpu(db, "s0", pool=GpuPool.MD)
    assert G.request_gpu(db, "s0", pool=GpuPool.MD) == gid     # idempotent
    row = next(r for r in G.list_gpus(db) if r.gpu_id == gid)
    assert row.running_count == 1
    assert G.release_gpu(db, "s0") is True
    assert G.release_gpu(db, "s0") is False                   # no double-free
    row = next(r for r in G.list_gpus(db) if r.gpu_id == gid)
    assert row.running_count == 0 and row.status == "available"


def test_release_frees_a_slot_for_a_waiting_claim():
    db = _fresh_db()
    # claim every MD slot, remembering which subjob took which GPU from the return values
    assigned = {f"s{i}": G.request_gpu(db, f"s{i}", pool=GpuPool.MD) for i in range(4)}
    assert all(g is not None for g in assigned.values())
    assert G.request_gpu(db, "s4", pool=GpuPool.MD) is None     # pool full
    # release one holder; a waiting claim then succeeds on the freed GPU
    holder, freed_gid = next(iter(assigned.items()))
    assert G.release_gpu(db, holder) is True
    assert G.request_gpu(db, "s4", pool=GpuPool.MD) == freed_gid


def test_set_gpu_pool_idle_reassign():
    db = _fresh_db()
    row = G.set_gpu_pool(db, 0, GpuPool.DESIGN)            # idle md GPU -> design
    assert row.pool == GpuPool.DESIGN and row.capacity == 1
    row = G.set_gpu_pool(db, 1, GpuPool.EXCLUDED)          # -> excluded => disabled
    assert row.pool == GpuPool.EXCLUDED and row.status == "disabled"
    assert G.set_gpu_pool(db, 999, GpuPool.MD) is None     # unknown GPU


def test_set_gpu_pool_busy_rejected():
    db = _fresh_db()
    gid = G.request_gpu(db, "s0", pool=GpuPool.MD)         # GPU now holds a running slot
    assert gid is not None
    with pytest.raises(G.GpuBusyError):
        G.set_gpu_pool(db, gid, GpuPool.DESIGN)            # must drain first
    # after release, reassignment succeeds
    G.release_gpu(db, "s0")
    assert G.set_gpu_pool(db, gid, GpuPool.DESIGN).pool == GpuPool.DESIGN


def test_runtime_concurrency_change_and_survives_reseed():
    db = _fresh_db()
    G.set_pool_capacity(db, GpuPool.MD, 3)
    assert all(r.capacity == 3 for r in G.list_gpus(db) if r.pool == GpuPool.MD)
    G.seed_gpus(db)  # simulate restart; env MD_GPU_CONCURRENCY=2 must NOT clobber the 3
    assert all(r.capacity == 3 for r in G.list_gpus(db) if r.pool == GpuPool.MD)


def test_capacity_lowered_below_running_count_stays_full():
    db = _fresh_db()
    # Fill BOTH MD GPUs to capacity 2 (4 claims spread 2+2 deterministically by least-loaded).
    for s in ("s0", "s1", "s2", "s3"):
        assert G.request_gpu(db, s, pool=GpuPool.MD) in (0, 1)
    md = [r for r in G.list_gpus(db) if r.pool == GpuPool.MD]
    assert all(r.running_count == 2 for r in md)
    # Lower capacity below running_count -> never evicts; stays BUSY; rejects new claims.
    G.set_pool_capacity(db, GpuPool.MD, 1)
    md = [r for r in G.list_gpus(db) if r.pool == GpuPool.MD]
    assert all(r.running_count == 2 and r.capacity == 1 and r.status == "busy" for r in md)
    assert G.request_gpu(db, "s4", pool=GpuPool.MD) is None   # pool full despite cap<rc


def test_reconcile_stale_slots_capped_at_capacity():
    db = _fresh_db()
    from app.models import JobStatus as JS, SubJob
    # Pin 3 active subjobs to gpu0 (capacity 2) and force a drifted running_count, then reconcile.
    for s in ("s0", "s1", "s2"):
        sj = db.get(SubJob, s)
        sj.assigned_gpu = 0
        sj.status = JS.RUNNING_MD
    row = next(r for r in G.list_gpus(db) if r.gpu_id == 0)
    row.running_count = 5  # simulate drift from a crashed worker
    db.commit()
    G.reconcile_running_counts(db)
    row = next(r for r in G.list_gpus(db) if r.gpu_id == 0)
    # recomputed from the 3 active subjobs, capped at capacity 2
    assert row.running_count == 2 and row.status == "busy"


def test_release_legacy_marker_path():
    db = _fresh_db()
    # Claim WITHOUT a SubJob row (legacy marker path): request_gpu for an id with no SubJob.
    gid = G.request_gpu(db, "no-such-subjob", pool=GpuPool.MD)
    assert gid is not None
    row = next(r for r in G.list_gpus(db) if r.gpu_id == gid)
    assert row.running_count == 1 and row.assigned_subjob_id == "no-such-subjob"
    assert G.release_gpu(db, "no-such-subjob") is True       # legacy decrement
    assert G.release_gpu(db, "no-such-subjob") is False      # no double-free
    row = next(r for r in G.list_gpus(db) if r.gpu_id == gid)
    assert row.running_count == 0 and row.assigned_subjob_id is None


def test_cancel_job_releases_gpu_slots():
    db = _fresh_db()
    from app.models import Job
    from app.services import jobs_service
    G.request_gpu(db, "s0", pool=GpuPool.MD)
    G.request_gpu(db, "s1", pool=GpuPool.MD)
    assert sum(r.running_count for r in G.list_gpus(db)) == 2
    job = db.get(Job, "j1")
    jobs_service.cancel_job(db, job)
    assert sum(r.running_count for r in G.list_gpus(db)) == 0   # no slot leak on cancel
    assert all(r.status != "busy" for r in G.list_gpus(db) if r.pool == GpuPool.MD)


def test_retry_job_releases_gpu_slots():
    db = _fresh_db()
    from app.models import Job, JobStatus as JS, SubJob
    from app.services import jobs_service
    # mark s0/s1 failed, holding GPUs, then retry -> slots freed, requeued
    for s in ("s0", "s1"):
        G.request_gpu(db, s, pool=GpuPool.MD)
        db.get(SubJob, s).status = JS.FAILED
    db.commit()
    assert sum(r.running_count for r in G.list_gpus(db)) == 2
    jobs_service.retry_job(db, db.get(Job, "j1"))
    assert sum(r.running_count for r in G.list_gpus(db)) == 0   # no slot leak on retry
    assert db.get(SubJob, "s0").assigned_gpu is None and db.get(SubJob, "s0").status == JS.QUEUED


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
