"""Startup seeding (CONTRACT §3): default admin + GPU rows."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Role, User
from .security import hash_password
from .services import gpu_manager


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
