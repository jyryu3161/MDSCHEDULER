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
    resp = client.post("/api/auth/login", json={"username": "csbl", "password": "csbl"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    # Seeded admin must be flagged for password change.
    assert body["must_change_password"] is True
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


def test_me_reports_must_change_password(client, admin_token):
    me = client.get("/api/auth/me", headers=_auth(admin_token))
    assert me.status_code == 200
    assert me.json()["username"] == "csbl"
    # Seeded admin starts flagged for a forced password change.
    assert me.json()["must_change_password"] is True


def _create_user(username: str, password: str, role: str = "user") -> None:
    """Insert a fresh user directly so the change-password test does not mutate the
    shared seeded admin (keeping tests order-independent)."""
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
                must_change_password=True,
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
