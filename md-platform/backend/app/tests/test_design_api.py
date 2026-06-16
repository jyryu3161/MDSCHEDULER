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
    # Login returns a usable token even while must_change_password is set (see test_api), so the
    # design API tests don't need to mutate the seeded admin's password.
    r = client.post("/api/auth/login", json={"username": "csbl", "password": "csbl"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_create_design_with_smiles(client):
    h = _auth(client)
    r = client.post("/api/design", headers=h, data={
        "name": "smiles design", "initial_sequences": "KCCIVYP, AAAAAAA GGGGGGG",
        "population_size": 6, "num_generations": 3, "top_k_md": 2,
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
