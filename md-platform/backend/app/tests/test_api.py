"""End-to-end backend API tests (CONTRACT §5/§6/§7).

Runs entirely on SQLite + QUEUE_BACKEND=local + MD_ENGINE=mock and never requires
GROMACS. The validation seam uses the real ``mdworker`` validator when installed; if
the worker is not importable, the chemistry/mapping assertions are skipped (the
auth/job-shape assertions still run), so this suite is robust whether or not the
worker package has been installed into the environment yet.

Run from the backend/ directory:  pytest -q
"""

from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path

import pytest

# Configure the environment BEFORE importing the app/config (cached settings).
_TMP = Path(tempfile.mkdtemp(prefix="mdplatform_test_"))
os.environ.update(
    {
        "STORAGE_ROOT": str(_TMP),
        "DATABASE_URL": f"sqlite:///{_TMP / 'test.db'}",
        "QUEUE_BACKEND": "local",
        "MD_ENGINE": "mock",
        "NUM_GPUS": "2",
        "JWT_SECRET": "test-secret",
        "DEFAULT_ADMIN_ID": "csbl",
        "DEFAULT_ADMIN_PASSWORD": "csbl",
        "REQUIRE_LIGAND_CHEMISTRY": "true",
    }
)

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import Base, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[3] / "samples"
PDBQT = SAMPLES / "3-HDC_KCCIVYP.pdbqt"
SDF = SAMPLES / "Structure2D_3-HDC.sdf"
RECEPTOR = SAMPLES / "fold_3_hdc_kccivyp_model_0.pdb"


def _worker_available() -> bool:
    try:
        importlib.import_module("mdworker.pipeline.steps.validate_input")
        return True
    except Exception:
        return False


WORKER = _worker_available()


@pytest.fixture(scope="module")
def client():
    # Fresh schema + seed (TestClient triggers the lifespan startup).
    Base.metadata.drop_all(bind=engine)
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client) -> str:
    # The server now blocks protected endpoints while must_change_password is set (forced-change),
    # so the seeded csbl admin's first-login token is NOT usable for the API tests. Create a
    # password-ready admin for those; csbl is left untouched for the auth-flow tests below.
    _create_user("apiadmin", "apipass-1", role="admin", must_change=False)
    resp = client.post("/api/auth/login", json={"username": "apiadmin", "password": "apipass-1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["must_change_password"] is False
    assert body["role"] == "admin"
    return body["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_login_bad_credentials(client):
    resp = client.post("/api/auth/login", json={"username": "csbl", "password": "wrong"})
    assert resp.status_code == 401


def test_me_reports_must_change_password(client):
    # The seeded csbl admin starts flagged for a forced password change. /auth/me is exempt from
    # the enforcement gate (it's one of the endpoints needed to complete the change), so it stays
    # reachable and reports the flag.
    login = client.post("/api/auth/login", json={"username": "csbl", "password": "csbl"})
    assert login.status_code == 200 and login.json()["must_change_password"] is True
    me = client.get("/api/auth/me", headers=_auth(login.json()["access_token"]))
    assert me.status_code == 200
    assert me.json()["username"] == "csbl"
    assert me.json()["must_change_password"] is True


def test_must_change_password_blocks_api_until_changed(client):
    # Server-side enforcement (not just the UI modal): a forced-change account can reach only the
    # password-change-flow endpoints; everything else is 403 until the password is changed.
    _create_user("pwlock", "initpass1", role="user", must_change=True)
    tok = client.post("/api/auth/login", json={"username": "pwlock", "password": "initpass1"}).json()["access_token"]
    h = _auth(tok)
    # Protected endpoints are blocked...
    assert client.get("/api/jobs", headers=h).status_code == 403
    assert client.get("/api/queue", headers=h).status_code == 403
    assert client.get("/api/dashboard/summary", headers=h).status_code == 403
    # ...but the change-password flow (me + change-password) is reachable.
    assert client.get("/api/auth/me", headers=h).status_code == 200
    cp = client.post("/api/auth/change-password", headers=h,
                     json={"old_password": "initpass1", "new_password": "newpass-99"})
    assert cp.status_code == 200, cp.text
    # After changing, a fresh token is fully usable.
    tok2 = client.post("/api/auth/login", json={"username": "pwlock", "password": "newpass-99"}).json()["access_token"]
    assert client.get("/api/jobs", headers=_auth(tok2)).status_code == 200


def test_must_change_password_blocks_realtime_auth(client):
    _create_user("rtlock", "initpass1", role="user", must_change=True)
    tok = client.post("/api/auth/login", json={"username": "rtlock", "password": "initpass1"}).json()["access_token"]
    h = _auth(tok)
    assert client.get("/api/events/dashboard", headers=h).status_code == 403

    from app.routers import ws
    assert ws._authenticate(tok) is None


def _create_user(username: str, password: str, role: str = "user", must_change: bool = True) -> None:
    """Insert a fresh user directly so the change-password test does not mutate the
    shared seeded admin (keeping tests order-independent). ``must_change`` controls the
    forced-password-change flag (default True; pass False for a ready-to-use account)."""
    from app.database import session_scope
    from app.models import User
    from app.security import hash_password

    with session_scope() as s:
        s.add(
            User(
                username=username,
                password_hash=hash_password(password),
                role=role,
                is_active=True,
                must_change_password=must_change,
            )
        )
        s.commit()


def test_change_password_clears_flag(client):
    # Use a dedicated, uniquely-named user (not the shared admin) so the test is
    # order-independent and safe to re-run against a persisted DB.
    import uuid

    username = f"pwuser_{uuid.uuid4().hex[:8]}"
    _create_user(username, "initpass1")
    login = client.post("/api/auth/login", json={"username": username, "password": "initpass1"})
    assert login.status_code == 200
    body = login.json()
    assert body["must_change_password"] is True
    token = body["access_token"]

    chg = client.post(
        "/api/auth/change-password",
        headers=_auth(token),
        json={"old_password": "initpass1", "new_password": "newpass-22"},
    )
    assert chg.status_code == 200, chg.text
    assert chg.json()["ok"] is True

    relog = client.post("/api/auth/login", json={"username": username, "password": "newpass-22"})
    assert relog.status_code == 200
    assert relog.json()["must_change_password"] is False


def test_unauthenticated_rejected(client):
    assert client.get("/api/dashboard/summary").status_code == 401


# ---------------------------------------------------------------------------
# Health / Dashboard / GPUs
# ---------------------------------------------------------------------------


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["engine"] == "mock"
    assert body["queue_backend"] == "local"


def test_dashboard_summary(client, admin_token):
    resp = client.get("/api/dashboard/summary", headers=_auth(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "total_jobs",
        "running_jobs",
        "queued_jobs",
        "completed_jobs",
        "failed_jobs",
        "gpus_available",
        "gpus_busy",
        "storage_used_gb",
        "storage_total_gb",
    ):
        assert key in body


def test_dashboard_summary_scopes_job_counts_to_user(client):
    import uuid

    from app.database import session_scope
    from app.models import InputType, Job, JobStatus, LigandType, User

    suffix = uuid.uuid4().hex[:8]
    username = f"dash_{suffix}"
    other_username = f"dash_other_{suffix}"
    _create_user(username, "dashpass-1", must_change=False)
    _create_user(other_username, "dashpass-1", must_change=False)

    with session_scope() as s:
        user = s.query(User).filter_by(username=username).one()
        other = s.query(User).filter_by(username=other_username).one()
        s.add_all(
            [
                Job(
                    id=f"{username}_queued",
                    user_id=user.id,
                    name="queued",
                    input_type=InputType.PDB,
                    ligand_type=LigandType.PEPTIDE,
                    status=JobStatus.QUEUED,
                ),
                Job(
                    id=f"{username}_running",
                    user_id=user.id,
                    name="running",
                    input_type=InputType.PDB,
                    ligand_type=LigandType.PEPTIDE,
                    status=JobStatus.RUNNING_MD,
                ),
                Job(
                    id=f"{username}_completed",
                    user_id=user.id,
                    name="completed",
                    input_type=InputType.PDB,
                    ligand_type=LigandType.PEPTIDE,
                    status=JobStatus.COMPLETED,
                ),
                Job(
                    id=f"{other_username}_failed",
                    user_id=other.id,
                    name="failed",
                    input_type=InputType.PDB,
                    ligand_type=LigandType.PEPTIDE,
                    status=JobStatus.FAILED,
                ),
            ]
        )
        s.commit()

    login = client.post("/api/auth/login", json={"username": username, "password": "dashpass-1"})
    assert login.status_code == 200, login.text
    resp = client.get("/api/dashboard/summary", headers=_auth(login.json()["access_token"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_jobs"] == 3
    assert body["queued_jobs"] == 1
    assert body["running_jobs"] == 1
    assert body["completed_jobs"] == 1
    assert body["failed_jobs"] == 0


def test_gpus_list(client, admin_token):
    resp = client.get("/api/gpus", headers=_auth(admin_token))
    assert resp.status_code == 200
    gpus = resp.json()
    # Seeded from nvidia-smi when present, else NUM_GPUS placeholders. Either way at
    # least one schedulable GPU row must exist.
    assert len(gpus) >= 1
    for g in gpus:
        assert "gpu_id" in g and "status" in g


# ---------------------------------------------------------------------------
# Upload + validate
# ---------------------------------------------------------------------------


def _upload(client, token, *, with_chem: bool) -> dict:
    files = {"pose_file": ("poses.pdbqt", PDBQT.read_bytes(), "chemical/x-pdbqt")}
    if with_chem:
        files["chemistry_file"] = ("chem.sdf", SDF.read_bytes(), "chemical/x-mdl-sdfile")
        files["receptor_file"] = ("receptor.pdb", RECEPTOR.read_bytes(), "chemical/x-pdb")
    resp = client.post("/api/uploads/input", headers=_auth(token), files=files)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.skipif(not WORKER, reason="mdworker validator not installed")
def test_upload_detects_nine_poses(client, admin_token):
    body = _upload(client, admin_token, with_chem=True)
    assert body["detected_pose_count"] == 9
    assert body["detected_input_type"] in ("pdbqt", "mixed", "pdb", "cif")

    val = client.get(f"/api/uploads/{body['upload_id']}/validate", headers=_auth(admin_token))
    assert val.status_code == 200, val.text
    report = val.json()
    assert report["pose_count"] == 9
    assert report["chem_source"] == "sdf"
    assert report["atom_mapping"]["success"] is True


# ---------------------------------------------------------------------------
# Job creation rules (CONTRACT §7)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not WORKER, reason="mdworker validator not installed")
def test_create_job_rejects_raw_pdbqt_only(client, admin_token):
    upload = _upload(client, admin_token, with_chem=False)
    resp = client.post(
        "/api/jobs",
        headers=_auth(admin_token),
        json={"upload_id": upload["upload_id"], "ligand_chem_source": "sdf", "md_preset": "standard"},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "CHEMISTRY_REQUIRED"
    assert "report" in detail


@pytest.mark.skipif(not WORKER, reason="mdworker validator not installed")
def test_create_job_succeeds_with_sdf(client, admin_token):
    upload = _upload(client, admin_token, with_chem=True)
    resp = client.post(
        "/api/jobs",
        headers=_auth(admin_token),
        json={
            "upload_id": upload["upload_id"],
            "name": "happy-path",
            "ligand_type": "small_molecule",
            "ligand_chem_source": "sdf",
            "top_n_poses": 3,
            "md_preset": "standard",
        },
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["md_length_ns"] == 50  # standard preset
    assert job["top_n_poses"] == 3
    job_id = job["id"]
    assert job_id.startswith("job_")

    # Job detail shows exactly 3 subjobs (top-n), sorted poses present.
    detail = client.get(f"/api/jobs/{job_id}", headers=_auth(admin_token))
    assert detail.status_code == 200, detail.text
    dbody = detail.json()
    assert len(dbody["subjobs"]) == 3
    for sj in dbody["subjobs"]:
        assert sj["id"] == f"{job_id}_pose_{sj['pose_index']:02d}"

    # metadata.json was written.
    settings = get_settings()
    meta = Path(settings.STORAGE_ROOT) / "jobs" / job_id / "metadata.json"
    assert meta.exists()

    # Queue snapshot is well-formed.
    q = client.get("/api/queue", headers=_auth(admin_token))
    assert q.status_code == 200
    assert "items" in q.json() and "running" in q.json()

    # Results endpoint is reachable even before completion.
    res = client.get(f"/api/jobs/{job_id}/results", headers=_auth(admin_token))
    assert res.status_code == 200
    assert len(res.json()["subjobs"]) == 3


def test_create_job_fans_out_replicas(client, admin_token):
    upload = _upload(client, admin_token, with_chem=True)
    resp = client.post(
        "/api/jobs",
        headers=_auth(admin_token),
        json={
            "upload_id": upload["upload_id"],
            "name": "replicas",
            "ligand_type": "small_molecule",
            "ligand_chem_source": "sdf",
            "top_n_poses": 2,
            "n_replicas": 2,
            "md_preset": "standard",
            # This test validates subjob fan-out (created at job-create time), not GPU
            # scheduling; run without GPU so its background mock subjobs don't claim GPU slots
            # and race the shared-DB gpu_pools tests.
            "use_gpu": False,
        },
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["n_replicas"] == 2
    job_id = job["id"]

    detail = client.get(f"/api/jobs/{job_id}", headers=_auth(admin_token)).json()
    # 2 poses × 2 replicas = 4 distinct subjobs.
    assert len(detail["subjobs"]) == 4
    ids = {sj["id"] for sj in detail["subjobs"]}
    assert len(ids) == 4
    # Both poses present; replica 1 keeps the canonical id, replica 2 gets the _rep_02 suffix.
    assert ids == {
        f"{job_id}_pose_01", f"{job_id}_pose_01_rep_02",
        f"{job_id}_pose_02", f"{job_id}_pose_02_rep_02",
    }
    by_pose: dict[int, set[int]] = {}
    for sj in detail["subjobs"]:
        by_pose.setdefault(sj["pose_index"], set()).add(sj["replica_index"])
    assert set(by_pose) == {1, 2}
    assert all(reps == {1, 2} for reps in by_pose.values())

    # replica_aggregates is present (entries appear once MM/GBSA exists; the structure is there).
    assert "replica_aggregates" in detail


def test_create_job_unknown_upload(client, admin_token):
    resp = client.post(
        "/api/jobs",
        headers=_auth(admin_token),
        json={"upload_id": "upload_does_not_exist", "md_preset": "standard"},
    )
    assert resp.status_code == 404


def test_jobs_list_scoping(client, admin_token):
    resp = client.get("/api/jobs?mine=true", headers=_auth(admin_token))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
