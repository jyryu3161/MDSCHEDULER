"""SQLAlchemy 2.0 engine + session management.

Works for both ``sqlite:///`` (local dev) and ``postgresql+psycopg://`` (compose).
SQLite needs ``check_same_thread=False``; the in-memory variant additionally needs
a ``StaticPool`` so every connection shares the same database.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _build_engine(database_url: str):
    """Create an Engine appropriate for the URL backend."""
    connect_args: dict = {}
    engine_kwargs: dict = {"pool_pre_ping": True, "future": True}

    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        # In-memory (or empty path) SQLite must share one connection.
        is_memory = ":memory:" in database_url or database_url.endswith("//")
        if is_memory:
            engine_kwargs["poolclass"] = StaticPool
        else:
            # Ensure the parent directory exists for file-backed sqlite.
            # URL forms: sqlite:///relative.db  or  sqlite:////abs/path.db
            path_part = database_url.split("sqlite:///", 1)[-1]
            if path_part and path_part != ":memory:":
                db_path = Path(path_part)
                db_path.parent.mkdir(parents=True, exist_ok=True)
        engine_kwargs.pop("pool_pre_ping", None)

    engine_kwargs["connect_args"] = connect_args
    return create_engine(database_url, **engine_kwargs)


_settings = get_settings()
engine = _build_engine(_settings.DATABASE_URL)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session)


def init_db() -> None:
    """Create all tables. Safe to call repeatedly (create_all is idempotent)."""
    # Import models so they register on Base.metadata before create_all.
    from . import models  # noqa: F401

    # Ensure storage root exists (file sqlite + artifact tree live under it).
    try:
        Path(_settings.STORAGE_ROOT).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a scoped session with guaranteed close."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def session_scope() -> Session:
    """Return a fresh Session for use outside the request lifecycle.

    Caller owns commit/rollback/close. Used by background tasks and the
    in-process LocalExecutor's DbReporter.
    """
    return SessionLocal()
