"""FastAPI dependencies for user authentication (cookie-based, multi-user)."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, status
from starlette.requests import HTTPConnection

from kimo_cli import logger
from kimo_cli.web.db.crud import get_user_session
from kimo_cli.web.db.database import get_db

_COOKIE_NAME = "kimo_session"


def user_from_connection(connection: HTTPConnection) -> dict[str, Any] | None:
    """Resolve the authenticated user behind any connection, or ``None``.

    Takes an ``HTTPConnection`` rather than a ``Request`` so a WebSocket
    handshake — which carries the very same cookies and headers — can be
    authenticated by exactly the same rule. Keeping the two apart is how a
    logged-in browser ended up authorized for every HTTP call and rejected on
    every socket.

    Authentication sources (in priority order):
    1. Cookie ``kimo_session`` (primary, used by browser clients).
    2. ``Authorization: Bearer <token>`` header (for API / admin clients).
    """
    token: str | None = None

    # Local-owner mode: a loopback deployment may run without a login page.
    # The local owner is returned as the identity so sessions and memory get
    # its stable owner_id instead of the unauthenticated sentinel.
    scope = getattr(connection, "scope", None)
    app = scope.get("app") if isinstance(scope, dict) else None
    if app is not None:
        # Local owner only applies when no bearer gate is configured — a
        # deployment that sets a token keeps its token auth. Reading it here
        # (not just in get_current_user) keeps every call site — the bearer
        # fallback in AuthMiddleware, /api/sessions, /api/memory — on the same
        # rule, so an owner_id is never lost to the sentinel by accident.
        session_token = getattr(app.state, "session_token", None)
        local_owner = getattr(app.state, "local_owner", None) if not session_token else None
        if local_owner:
            # Tag the identity so /api/auth/me can tell the frontend this is
            # the no-login local owner (no sign-out entry point makes sense).
            return {**local_owner, "auth_mode": "local"}

    # 1. Cookie
    cookie_token = connection.cookies.get(_COOKIE_NAME)
    if cookie_token:
        token = cookie_token

    # 2. Bearer header fallback
    if not token:
        auth_header = connection.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip() or None

    if not token:
        return None

    try:
        db = get_db()
        try:
            user = get_user_session(db, token)
        finally:
            db.close()
        return user
    except Exception as exc:
        logger.warning("Failed to resolve user session: {err}", err=exc)
        return None


def get_current_user(request: Request) -> dict[str, Any] | None:
    """Return the currently authenticated user dict, or ``None``."""
    return user_from_connection(request)


def require_current_user(
    user: dict[str, Any] | None = Depends(get_current_user),  # noqa: B008
) -> dict[str, Any]:
    """Require an authenticated user; raise 401 if not logged in."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return user


def require_admin(
    user: dict[str, Any] = Depends(require_current_user),  # noqa: B008
) -> dict[str, Any]:
    """Require admin role; raise 403 if the user is not an admin."""
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


def get_current_user_sse(request: Request) -> dict[str, Any] | None:
    """Same as :func:`get_current_user` but also accepts a ``?token=`` query param.

    Browser ``EventSource`` cannot send custom headers, so SSE consumers pass
    the token in the URL. Cookie auth still wins when both are present.
    """
    user = get_current_user(request)
    if user is not None:
        return user
    query_token = request.query_params.get("token")
    if not query_token:
        return None
    try:
        db = get_db()
        try:
            return get_user_session(db, query_token)
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Failed to resolve SSE user session: {err}", err=exc)
        return None


def require_current_user_sse(
    user: dict[str, Any] | None = Depends(get_current_user_sse),  # noqa: B008
) -> dict[str, Any]:
    """SSE-friendly variant of :func:`require_current_user`."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return user


__all__ = [
    "get_current_user",
    "get_current_user_sse",
    "require_admin",
    "require_current_user",
    "require_current_user_sse",
]
