"""Admin report-settings API: get/set the Gemini key+model, masking, persistence, admin-only."""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="mdplatform_settings_api_")
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


@pytest.fixture(scope="module")
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _user(username: str, password: str, role: str) -> None:
    from app.models import User
    from app.security import hash_password
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == username).first() is None:
            db.add(User(username=username, password_hash=hash_password(password),
                        role=role, is_active=True, must_change_password=False))
            db.commit()
    finally:
        db.close()


def _token(client, username, password) -> dict:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_get_set_report_settings_admin(client):
    _user("setadmin", "setpass-1", "admin")
    h = _token(client, "setadmin", "setpass-1")

    # Default: no DB key set -> source env or none, key not echoed.
    r = client.get("/api/settings/report", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "gemini_model" in body and body["key_source"] in ("env", "none")
    assert "gemini_api_key" not in body  # plaintext key never returned

    # Set a key + model.
    r = client.put("/api/settings/report", headers=h,
                   json={"gemini_api_key": "AIzaSECRETKEY1234", "gemini_model": "gemini-3.5-flash"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["key_set"] is True and body["key_source"] == "db"
    assert body["gemini_model"] == "gemini-3.5-flash"
    assert body["key_masked"].endswith("1234") and "SECRET" not in body["key_masked"]

    # Persisted on re-fetch; still masked, never plaintext.
    body2 = client.get("/api/settings/report", headers=h).json()
    assert body2["key_set"] is True and body2["key_masked"].endswith("1234")

    # Clearing the key (empty string) falls back to env/none.
    r = client.put("/api/settings/report", headers=h, json={"gemini_api_key": ""})
    assert r.json()["key_source"] in ("env", "none")

    # The worker config resolver reflects the DB value.
    db = SessionLocal()
    try:
        from app.services import settings_store
        settings_store.set_value(db, settings_store.KEY_GEMINI_API, "AIzaDBKEYABCD")
        key, model = settings_store.gemini_config(db)
        assert key == "AIzaDBKEYABCD"
    finally:
        db.close()


def test_report_settings_admin_only(client):
    _user("setuser", "userpass-1", "user")
    h = _token(client, "setuser", "userpass-1")
    assert client.get("/api/settings/report", headers=h).status_code == 403
    assert client.put("/api/settings/report", headers=h, json={"gemini_model": "x"}).status_code == 403


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
