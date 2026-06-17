"""Admin settings — runtime configuration of the Gemini auto-report key/model.

Admin-only. The API never returns the API key in plaintext; it returns a masked preview, a
"set" flag, and the source (db | env | none). Writing an empty string clears the DB override
(falling back to the environment); omitting a field leaves it unchanged."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_admin
from ..models import User
from ..services import settings_store

router = APIRouter(prefix="/settings", tags=["settings"])


class ReportSettingsOut(BaseModel):
    gemini_model: str
    key_set: bool
    key_masked: str
    key_source: str  # db | env | none


class ReportSettingsIn(BaseModel):
    # None = leave unchanged; "" = clear the DB override (fall back to env); value = set.
    gemini_api_key: str | None = None
    gemini_model: str | None = None


def _mask(key: str) -> str:
    if not key:
        return ""
    return ("•" * 4 + key[-4:]) if len(key) > 4 else "••••"


def _current(db: Session) -> ReportSettingsOut:
    api_key, model = settings_store.gemini_config(db)
    return ReportSettingsOut(gemini_model=model, key_set=bool(api_key),
                             key_masked=_mask(api_key), key_source=settings_store.gemini_key_source(db))


@router.get("/report", response_model=ReportSettingsOut)
def get_report_settings(db: Session = Depends(get_db), _admin: User = Depends(require_admin)) -> ReportSettingsOut:
    return _current(db)


@router.put("/report", response_model=ReportSettingsOut)
def put_report_settings(body: ReportSettingsIn, db: Session = Depends(get_db),
                        _admin: User = Depends(require_admin)) -> ReportSettingsOut:
    if body.gemini_api_key is not None:
        settings_store.set_value(db, settings_store.KEY_GEMINI_API, body.gemini_api_key.strip())
    if body.gemini_model is not None and body.gemini_model.strip():
        settings_store.set_value(db, settings_store.KEY_GEMINI_MODEL, body.gemini_model.strip())
    return _current(db)
