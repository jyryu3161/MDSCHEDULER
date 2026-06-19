"""Design API tests: create (file + SMILES), validation, list scoping, detail
leaderboard/convergence curve, and cooperative cancel. The worker run is stubbed (enqueue is
patched to a no-op) so these exercise the HTTP/DB layer without Vina/GROMACS."""

from __future__ import annotations

import io
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="mdplatform_design_api_")
os.environ.setdefault("STORAGE_ROOT", _TMP)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/test.db")
os.environ.setdefault("QUEUE_BACKEND", "local")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("DEFAULT_ADMIN_ID", "csbl")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "csbl")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import DesignCandidate, DesignJob  # noqa: E402


@pytest.fixture(scope="module")
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _no_enqueue(monkeypatch):
    # Don't actually run the GA worker during API tests.
    from app.services.queue_manager import QueueManager
    monkeypatch.setattr(QueueManager, "enqueue_design", lambda self, design_id: None)


def _auth(client):
    # The server now blocks protected endpoints while must_change_password is set, so the seeded
    # csbl admin's first-login token is not usable. Use a password-ready admin instead (idempotent
    # for the module's shared DB); admin role so the admin-sees-all scoping test holds.
    from app.models import User
    from app.security import hash_password
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == "apiadmin").first() is None:
            db.add(User(username="apiadmin", password_hash=hash_password("apipass-1"),
                        role="admin", is_active=True, must_change_password=False))
            db.commit()
    finally:
        db.close()
    r = client.post("/api/auth/login", json={"username": "apiadmin", "password": "apipass-1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_create_design_with_smiles(client):
    h = _auth(client)
    r = client.post("/api/design", headers=h, data={
        "name": "smiles design", "initial_sequences": "KCCIVYP, AAAAAAA GGGGGGG",
        "population_size": 6, "num_generations": 3, "dock_oversample": 2,
        "md_length_ns": 10, "smiles": "CC(=O)Oc1ccccc1C(=O)O", "compound_name": "aspirin",
    })
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["status"] == "queued" and j["peptide_length"] == 7
    assert j["population_size"] == 6 and j["num_generations"] == 3


def test_create_design_with_file(client):
    h = _auth(client)
    sdf = b"\n  dummy\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n$$$$\n"
    r = client.post("/api/design", headers=h,
                    data={"name": "file design", "initial_sequences": "IIWWYYP"},
                    files={"compound": ("c.sdf", io.BytesIO(sdf), "chemical/x-mdl-sdfile")})
    assert r.status_code == 201, r.text
    assert r.json()["peptide_length"] == 7


def test_mixed_length_sequences_rejected(client):
    h = _auth(client)
    r = client.post("/api/design", headers=h, data={
        "name": "bad", "initial_sequences": "KCCIVYP, AAAA", "smiles": "C",
    })
    assert r.status_code == 422


def test_missing_compound_rejected(client):
    h = _auth(client)
    r = client.post("/api/design", headers=h, data={"name": "no compound", "initial_sequences": "KCCIVYP"})
    assert r.status_code == 400


def test_detail_leaderboard_and_convergence_curve(client):
    h = _auth(client)
    r = client.post("/api/design", headers=h,
                    data={"name": "curve", "initial_sequences": "KCCIVYP", "smiles": "C"})
    design_id = r.json()["id"]

    # Seed candidates directly: gen 0 best dock -5 (fitness 5), gen 1 a refined ΔG -8 (fitness 8).
    db = SessionLocal()
    try:
        db.add_all([
            DesignCandidate(design_job_id=design_id, generation=0, sequence="KCCIVYP",
                            docking_score=-5.0, fitness=5.0, refined=False),
            DesignCandidate(design_job_id=design_id, generation=0, sequence="AAAAAAA",
                            docking_score=-3.0, fitness=3.0, refined=False),
            DesignCandidate(design_job_id=design_id, generation=1, sequence="IIWWYYP",
                            docking_score=-6.0, md_dg=-8.0, fitness=8.0, refined=True),
        ])
        db.commit()
    finally:
        db.close()

    detail = client.get(f"/api/design/{design_id}", headers=h).json()
    # leaderboard sorted by fitness desc
    assert [c["sequence"] for c in detail["candidates"]] == ["IIWWYYP", "KCCIVYP", "AAAAAAA"]
    assert detail["candidates"][0]["refined"] is True and detail["candidates"][0]["md_dg"] == -8.0
    # convergence curve is best-so-far, monotonic non-decreasing
    fits = [p["best_fitness"] for p in detail["generations"]]
    assert fits == [5.0, 8.0] and fits == sorted(fits)


def test_cancel_design(client):
    h = _auth(client)
    r = client.post("/api/design", headers=h,
                    data={"name": "cancel me", "initial_sequences": "KCCIVYP", "smiles": "C"})
    design_id = r.json()["id"]
    c = client.post(f"/api/design/{design_id}/cancel", headers=h)
    assert c.status_code == 200 and c.json()["status"] == "cancelled"


def test_delete_design_requires_terminal(client):
    h = _auth(client)
    r = client.post("/api/design", headers=h,
                    data={"name": "delete me", "initial_sequences": "KCCIVYP", "smiles": "C"})
    design_id = r.json()["id"]
    # Seed a candidate so we can confirm children are removed too.
    db = SessionLocal()
    try:
        db.add(DesignCandidate(design_job_id=design_id, generation=0, sequence="KCCIVYP",
                               docking_score=-5.0, fitness=5.0, refined=False))
        db.commit()
    finally:
        db.close()

    # A queued (non-terminal) run cannot be deleted -> 409.
    assert client.delete(f"/api/design/{design_id}", headers=h).status_code == 409

    # Cancel makes it terminal; delete then succeeds and removes row + candidates.
    assert client.post(f"/api/design/{design_id}/cancel", headers=h).status_code == 200
    d = client.delete(f"/api/design/{design_id}", headers=h)
    assert d.status_code == 200 and d.json()["ok"] is True
    assert client.get(f"/api/design/{design_id}", headers=h).status_code == 404
    db = SessionLocal()
    try:
        assert db.get(DesignJob, design_id) is None
        assert db.query(DesignCandidate).filter(
            DesignCandidate.design_job_id == design_id).count() == 0
    finally:
        db.close()


def test_delete_design_unknown_404(client):
    h = _auth(client)
    assert client.delete("/api/design/design_99999999_999", headers=h).status_code == 404


def test_eval_mode_and_dock_engine_round_trip(client):
    h = _auth(client)
    j = client.post("/api/design", headers=h, data={
        "name": "md-only smina run", "initial_sequences": "KCCIVYP",
        "smiles": "C", "eval_mode": "md_only", "dock_engine": "smina",
    }).json()
    assert j["eval_mode"] == "md_only" and j["dock_engine"] == "smina"
    # defaults when omitted
    d = client.post("/api/design", headers=h,
                    data={"name": "defaults", "initial_sequences": "KCCIVYP", "smiles": "C"}).json()
    assert d["eval_mode"] == "hybrid" and d["dock_engine"] == "vina"


def test_invalid_dock_engine_rejected(client):
    h = _auth(client)
    # ADCP etc. are peptide-into-protein tools, not valid small-molecule docking engines.
    r = client.post("/api/design", headers=h, data={
        "name": "bad engine", "initial_sequences": "KCCIVYP", "smiles": "C", "dock_engine": "adcp",
    })
    assert r.status_code == 422


def _make_user(username: str, password: str) -> None:
    from app.models import User
    from app.security import hash_password
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == username).first() is None:
            db.add(User(username=username, password_hash=hash_password(password),
                        role="user", is_active=True, must_change_password=False))
            db.commit()
    finally:
        db.close()


def test_design_list_scoping_admin_sees_all(client):
    admin = _auth(client)  # seeded admin "csbl"
    _make_user("designer_bob", "bobpass-1")
    tok = client.post("/api/auth/login",
                      json={"username": "designer_bob", "password": "bobpass-1"}).json()["access_token"]
    bob = {"Authorization": f"Bearer {tok}"}
    bob_design = client.post("/api/design", headers=bob,
                             data={"name": "bob run", "initial_sequences": "KCCIVYP", "smiles": "C"}).json()["id"]

    # Bob sees his own design; admin sees Bob's design too (admin-sees-all scoping)
    assert any(d["id"] == bob_design for d in client.get("/api/design", headers=bob).json())
    admin_ids = {d["id"] for d in client.get("/api/design", headers=admin).json()}
    assert bob_design in admin_ids
    # Bob can fetch his own design detail
    assert client.get(f"/api/design/{bob_design}", headers=bob).status_code == 200


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
