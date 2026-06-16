"""Password hashing (bcrypt) and JWT token helpers (PyJWT)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from .config import get_settings

_ALGORITHM = "HS256"
# bcrypt operates on at most 72 bytes. Rather than silently truncate (which lets
# distinct long passwords collide) we reject longer inputs and let callers map the
# error to a 422/400. This keeps two distinct >72-byte passwords from hashing equal.
_BCRYPT_MAX_BYTES = 72

# Claims the server controls; callers may not override these via `extra`.
_RESERVED_CLAIMS = frozenset({"sub", "uid", "role", "iat", "exp", "nbf", "iss", "aud", "jti"})


class PasswordTooLongError(ValueError):
    """Raised when a password exceeds bcrypt's 72-byte limit."""


def _to_bcrypt_bytes(password: str) -> bytes:
    raw = password.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_BYTES:
        raise PasswordTooLongError(
            f"Password exceeds the {_BCRYPT_MAX_BYTES}-byte limit (got {len(raw)} bytes)."
        )
    return raw


def hash_password(password: str) -> str:
    """Return a bcrypt hash (utf-8 string) for the given plaintext password.

    Raises PasswordTooLongError if the password exceeds 72 bytes.
    """
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(_to_bcrypt_bytes(password), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time verification of a plaintext password against a stored hash."""
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(_to_bcrypt_bytes(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(*, subject: str, user_id: int, role: str, extra: dict[str, Any] | None = None) -> str:
    """Create a signed JWT access token.

    ``subject`` is the username; ``user_id`` and ``role`` are embedded as claims.
    Expiry is JWT_EXPIRE_MINUTES from now.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "uid": user_id,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)).timestamp()),
    }
    if extra:
        # Never let extra claims override server-controlled identity/expiry claims.
        safe_extra = {k: v for k, v in extra.items() if k not in _RESERVED_CLAIMS}
        payload.update(safe_extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT. Raises jwt exceptions on failure."""
    settings = get_settings()
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[_ALGORITHM])
