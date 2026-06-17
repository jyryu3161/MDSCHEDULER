"""Startup seeding (CONTRACT §3): default admin + GPU rows."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sqlalchemy import inspect, text

from .config import get_settings
from .models import Role, User
from .security import hash_password
from .services import gpu_manager


def ensure_design_columns(db: Session) -> None:
    """Add columns to a pre-existing designjobs table (create_all only creates missing tables,
    not missing columns). No-op once present; SQLite + PostgreSQL both support ADD COLUMN."""
    bind = db.get_bind()
    if "designjobs" not in inspect(bind).get_table_names():
        return
    existing = {c["name"] for c in inspect(bind).get_columns("designjobs")}
    ddl = {
        "eval_mode": "ALTER TABLE designjobs ADD COLUMN eval_mode VARCHAR(16) NOT NULL DEFAULT 'hybrid'",
        "dock_engine": "ALTER TABLE designjobs ADD COLUMN dock_engine VARCHAR(16) NOT NULL DEFAULT 'vina'",
        "n_replicas": "ALTER TABLE designjobs ADD COLUMN n_replicas INTEGER NOT NULL DEFAULT 1",
    }
    changed = False
    for col, stmt in ddl.items():
        if col not in existing:
            db.execute(text(stmt))
            changed = True
    if changed:
        db.commit()


def ensure_subjob_columns(db: Session) -> None:
    """Add the replica_index column to a pre-existing subjobs table (default 1 = single replica),
    so older databases keep working after the MD-replicas change. No-op once present."""
    bind = db.get_bind()
    if "subjobs" not in inspect(bind).get_table_names():
        return
    existing = {c["name"] for c in inspect(bind).get_columns("subjobs")}
    changed = False
    if "replica_index" not in existing:
        db.execute(text("ALTER TABLE subjobs ADD COLUMN replica_index INTEGER NOT NULL DEFAULT 1"))
        changed = True
    job_cols = {c["name"] for c in inspect(bind).get_columns("jobs")}
    if "n_replicas" not in job_cols:
        db.execute(text("ALTER TABLE jobs ADD COLUMN n_replicas INTEGER NOT NULL DEFAULT 1"))
        changed = True
    if changed:
        db.commit()


def seed_admin(db: Session) -> None:
    """Create the default admin (must_change_password=True) if no users exist."""
    settings = get_settings()
    user_count = db.execute(select(func.count()).select_from(User)).scalar_one()
    if user_count and int(user_count) > 0:
        return
    admin = User(
        username=settings.DEFAULT_ADMIN_ID,
        password_hash=hash_password(settings.DEFAULT_ADMIN_PASSWORD),
        role=Role.ADMIN,
        is_active=True,
        must_change_password=True,
    )
    db.add(admin)
    db.commit()


def seed_all(db: Session) -> None:
    seed_admin(db)
    gpu_manager.seed_gpus(db)
    ensure_design_columns(db)
    ensure_subjob_columns(db)
