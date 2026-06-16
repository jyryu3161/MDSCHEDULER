"""Auth router (CONTRACT §5 Auth)."""

from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    OkResponse,
    UserMe,
)
from ..security import (
    PasswordTooLongError,
    create_access_token,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Login rate limiter: max 10 FAILED attempts/min per username (CONTRACT §5).
#
# Scope/limitations (documented honestly):
#   * In-process, best-effort. Protects a single backend process (the MVP/compose
#     topology runs one backend container). NOT shared across replicas or restarts;
#     a horizontally-scaled or IP-based limiter is an explicit production follow-up
#     and is intentionally out of the per-username scope the contract specifies.
#   * Memory is bounded by pruning expired windows (60s TTL) plus a soft cap. The
#     cap is sized so that, under the single-process MVP, it is effectively
#     unreachable during normal operation while still capping worst-case memory at
#     a few MB. At capacity we prune all expired entries first; a brand-new username
#     (which by definition has <10 recent failures) is allowed through rather than
#     429-ing a legitimate first-time login — failing open here is the contract-
#     correct choice because the per-username guarantee only constrains accounts
#     that have already accrued failures.
_RATE_WINDOW_SECONDS = 60.0
_RATE_MAX_FAILURES = 10
_RATE_MAX_TRACKED_USERS = 100000
_failures: dict[str, deque[float]] = {}
_failures_lock = threading.Lock()


def _prune_locked(username: str, now: float) -> deque[float] | None:
    """Prune expired timestamps for one username (caller holds the lock).

    Returns the (non-empty) deque, or None when it became empty and was evicted.
    """
    dq = _failures.get(username)
    if dq is None:
        return None
    while dq and now - dq[0] > _RATE_WINDOW_SECONDS:
        dq.popleft()
    if not dq:
        _failures.pop(username, None)
        return None
    return dq


def _prune_all_locked(now: float) -> None:
    """Drop fully-expired entries across all tracked usernames (caller holds lock)."""
    stale = [u for u, dq in _failures.items() if not dq or now - dq[-1] > _RATE_WINDOW_SECONDS]
    for u in stale:
        _failures.pop(u, None)


def _record_failure(username: str) -> None:
    now = time.monotonic()
    with _failures_lock:
        if username not in _failures and len(_failures) >= _RATE_MAX_TRACKED_USERS:
            # First reclaim memory by dropping only entries that are already expired
            # (never an actively-limited one). If still at capacity, refuse to track
            # this NEW username — failing open for it rather than evicting a victim
            # that is currently being protected by the limiter.
            _prune_all_locked(now)
            if len(_failures) >= _RATE_MAX_TRACKED_USERS:
                return
        dq = _failures.setdefault(username, deque())
        dq.append(now)
        while dq and now - dq[0] > _RATE_WINDOW_SECONDS:
            dq.popleft()


def _is_rate_limited(username: str) -> bool:
    now = time.monotonic()
    with _failures_lock:
        dq = _prune_locked(username, now)
        return dq is not None and len(dq) >= _RATE_MAX_FAILURES


def _clear_failures(username: str) -> None:
    with _failures_lock:
        _failures.pop(username, None)


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    username = body.username.strip()

    if _is_rate_limited(username):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again in a minute.",
        )

    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        _record_failure(username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password.")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is inactive.")

    _clear_failures(username)
    token = create_access_token(subject=user.username, user_id=user.id, role=user.role)
    return LoginResponse(
        access_token=token,
        token_type="bearer",
        must_change_password=user.must_change_password,
        role=user.role,
        username=user.username,
    )


@router.post("/logout", response_model=OkResponse)
def logout(_user: User = Depends(get_current_user)) -> OkResponse:
    # Stateless JWT: the client discards the token. Endpoint exists for symmetry.
    return OkResponse(ok=True)


@router.post("/change-password", response_model=OkResponse)
def change_password(
    body: ChangePasswordRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OkResponse:
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect.")
    if body.new_password == body.old_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must differ from the current password.",
        )
    try:
        user.password_hash = hash_password(body.new_password)
    except PasswordTooLongError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    user.must_change_password = False
    db.commit()
    return OkResponse(ok=True)


@router.get("/me", response_model=UserMe)
def me(user: User = Depends(get_current_user)) -> UserMe:
    return UserMe.model_validate(user)
