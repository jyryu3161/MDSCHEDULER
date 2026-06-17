"""FastAPI dependencies: DB session, current user, admin guard, internal token."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import get_db
from .models import Role, User
from .security import decode_access_token

# auto_error=False so we can return our own 401 shape with WWW-Authenticate.
_bearer = HTTPBearer(auto_error=False)


def get_app_settings() -> Settings:
    return get_settings()


def _authenticate(
    credentials: HTTPAuthorizationCredentials | None,
    db: Session,
) -> User:
    """Resolve the authenticated user from the Bearer JWT, or 401. Does NOT enforce the
    must-change-password gate (see get_current_user vs get_current_user_allow_password_change)."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_access_token(credentials.credentials)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    uid = payload.get("uid")
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token.")

    user = db.get(User, uid_int)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive.")
    return user


def get_current_user_allow_password_change(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """Authenticated user WITHOUT the must-change-password gate. Use ONLY for the endpoints a
    user must reach while their password change is still pending: change-password, me, logout."""
    return _authenticate(credentials, db)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """Authenticated user for all protected endpoints. Default-deny: if the account still has a
    pending forced password change, every protected endpoint is blocked with 403 (the UI modal is
    only a convenience — this is the server-side enforcement). The three password-change-flow
    endpoints opt out via get_current_user_allow_password_change."""
    user = _authenticate(credentials, db)
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change required before using the API.",
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Allow only admin users; 403 otherwise."""
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator privileges required.")
    return user


def verify_internal_token(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Guard internal worker->backend endpoints by shared token."""
    if not x_internal_token or x_internal_token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid internal token.")
