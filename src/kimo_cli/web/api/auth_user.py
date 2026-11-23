"""User authentication API endpoints (login / logout / me)."""

from __future__ import annotations

import os
import secrets
import threading
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.requests import Request
from pydantic import BaseModel

from kimo_cli.utils.logging import logger
from kimo_cli.web.db.crud import (
    create_user_session,
    delete_user_session,
    get_user_by_username,
    hash_password,
    verify_password,
)
from kimo_cli.web.db.database import db_session
from kimo_cli.web.session_policy import COOKIE_NAME as _COOKIE_NAME
from kimo_cli.web.session_policy import session_cookie_secure, session_max_age
from kimo_cli.web.user_auth import require_current_user, session_token_from_connection

router = APIRouter(prefix="/api/auth", tags=["auth"])

_DEFAULT_LOGIN_MAX_CONCURRENT = 2


def _login_max_concurrent() -> int:
    """Per-process KDF concurrency; zero/invalid values use the safe default."""
    try:
        value = int(os.environ.get("KIMO_WEB_LOGIN_MAX_CONCURRENT", ""))
    except ValueError:
        return _DEFAULT_LOGIN_MAX_CONCURRENT
    return value if value > 0 else _DEFAULT_LOGIN_MAX_CONCURRENT


# This is deliberately a small resource bound, not a claim of global/IP rate
# limiting. Each uvicorn worker owns one semaphore, so N workers permit N times
# this many KDFs. Deployments with multiple workers still need edge/proxy limits
# for a service-wide request rate or per-account/per-IP policy.
_LOGIN_KDF_SLOTS = threading.BoundedSemaphore(_login_max_concurrent())


class LoginRequest(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    user_id: str
    username: str
    role: str
    #: "local" when the identity is the no-login local owner, "password"
    #: for a real cookie/bearer session. The frontend hides sign-out for
    #: the local owner.
    auth_mode: str = "password"


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """A hash of nothing, to spend the same time on a username that is not there.

    Computed on first use rather than at import so a server start does not pay
    for a bcrypt round it may never need.
    """
    return hash_password(secrets.token_urlsafe(32))


@router.post("/login", summary="Log in and obtain a session cookie")
def login(body: LoginRequest, response: Response) -> UserResponse:
    """Authenticate with username and password.

    On success, sets an ``HttpOnly`` ``kimo_session`` cookie and returns basic
    user information. Returns 401 on invalid credentials or inactive account.

    A plain ``def``, not ``async def``: everything in here is blocking, and the
    expensive part is deliberately so. Verifying a password is a KDF — bcrypt
    when passlib is present, PBKDF2 at 600k rounds when it is not, which is a
    tenth of a second on a laptop and closer to half on a container vCPU — and
    the equal-time path below now pays it for an unknown username too. On the
    event loop that is a login endpoint anyone can use to stall every socket,
    stream and request in the process. FastAPI runs a sync handler in its
    threadpool, which is where this belongs. A non-blocking, per-process
    semaphore bounds how many of those expensive operations can run at once;
    excess requests receive 429 instead of accumulating in an unbounded queue.
    """
    with db_session() as db:
        user = get_user_by_username(db, body.username)
        if not _LOGIN_KDF_SLOTS.acquire(blocking=False):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts in progress; retry shortly",
                headers={"Retry-After": "1"},
            )
        try:
            if user is None:
                # Pay for a hash anyway. Returning without one answers an unknown
                # username in microseconds while a known one costs a full bcrypt
                # round — the responses are identical, but the clock says which
                # accounts exist.
                password_valid = verify_password(body.password, _dummy_password_hash())
            else:
                password_valid = verify_password(body.password, user["password_hash"])
        finally:
            _LOGIN_KDF_SLOTS.release()
        if user is None or not password_valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
            )
        if not user.get("is_active"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Account is disabled",
            )
        max_age = session_max_age()
        token = create_user_session(db, user["id"], expires_in_seconds=max_age)

    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=session_cookie_secure(),
        samesite="lax",
        path="/",
        max_age=max_age,
    )
    return UserResponse(
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        auth_mode=user.get("auth_mode", "password"),
    )


@router.post("/logout", summary="Log out and clear the session cookie")
def logout(request: Request, response: Response) -> dict[str, str]:
    """Invalidate the current cookie/Bearer session and clear the cookie."""
    token = session_token_from_connection(request)
    if token:
        try:
            with db_session() as db:
                delete_user_session(db, token)
        except Exception:
            # The failed deletion can leave the token valid. Never include the
            # token, a prefix/hash derived for identification, or exception text
            # that might quote it in logs: any of those can aid replay.
            logger.warning("Failed to delete the current session during logout")

    response.delete_cookie(key=_COOKIE_NAME, path="/", secure=session_cookie_secure())
    return {"detail": "Logged out"}


@router.get("/me", summary="Get current authenticated user")
async def me(
    user: dict[str, Any] = Depends(require_current_user),
) -> UserResponse:
    """Return the currently authenticated user. Returns 401 if not logged in."""
    return UserResponse(
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
        auth_mode=user.get("auth_mode", "password"),
    )


__all__ = ["router"]
