"""Admin-editable runtime settings (the ``appsettings`` table) with environment fallback.

Currently holds the Gemini report API key + model. A value set from the Admin tab is stored in
the DB and takes precedence over the environment, so an operator can configure or rotate the key
without redeploying. The key is never returned to clients in plaintext (the router masks it)."""

from __future__ import annotations

from typing import Optional, Tuple

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AppSetting

KEY_GEMINI_API = "gemini_api_key"
KEY_GEMINI_MODEL = "gemini_model"


def get(db: Session, key: str) -> Optional[str]:
    row = db.get(AppSetting, key)
    return row.value if row is not None else None


def set_value(db: Session, key: str, value: Optional[str]) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def gemini_config(db: Session) -> Tuple[str, str]:
    """Return (api_key, model): the DB value when set, else the environment default."""
    s = get_settings()
    db_key = get(db, KEY_GEMINI_API)
    db_model = get(db, KEY_GEMINI_MODEL)
    api_key = db_key if db_key else (s.GEMINI_API_KEY or "")
    model = db_model if db_model else (s.GEMINI_MODEL or "gemini-3.5-flash")
    return api_key, model


def gemini_key_source(db: Session) -> str:
    """Where the active key comes from: 'db' | 'env' | 'none'."""
    if get(db, KEY_GEMINI_API):
        return "db"
    if get_settings().GEMINI_API_KEY:
        return "env"
    return "none"
