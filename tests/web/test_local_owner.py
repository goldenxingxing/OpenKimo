"""Local-owner mode: a loopback deployment runs without a login page.

``KIMO_WEB_LOCAL_OWNER`` (or ``create_app(local_owner=True)``) provisions a
stable ``local`` admin user and lets ``user_from_connection`` resolve it for a
loopback deployment, so the browser never sees a login form.  A deployment that
configures a session token keeps its token gate; local owner only applies when
no token is configured.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from kimo_cli.web.app import create_app
from kimo_cli.web.db.database import ensure_local_owner, init_db
from kimo_cli.web.user_auth import user_from_connection

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def test_ensure_local_owner_is_stable_and_admin() -> None:
    """The owner is one row, admin-role, and repeated calls return the same id."""
    init_db()
    first = ensure_local_owner()
    second = ensure_local_owner()

    assert first is not None and second is not None
    assert first["id"] == second["id"]
    assert first["username"] == "local"
    assert first["role"] == "admin"
    assert first["is_active"] == 1


# ---------------------------------------------------------------------------
# user_from_connection (dependency-level)
# ---------------------------------------------------------------------------


class _State:
    def __init__(self, local_owner, session_token) -> None:
        self.local_owner = local_owner
        self.session_token = session_token


class _App:
    def __init__(self, local_owner, session_token) -> None:
        self.state = _State(local_owner, session_token)


class _Req:
    def __init__(self, local_owner, session_token) -> None:
        self.scope = {"app": _App(local_owner, session_token)}
        self.cookies: dict[str, str] = {}
        self.headers: dict[str, str] = {}


def test_user_from_connection_returns_local_owner() -> None:
    owner = {"id": "local", "role": "admin", "is_active": 1}
    result = user_from_connection(_Req(owner, session_token=None))
    # The identity is returned as a tagged copy (auth_mode="local") so
    # /api/auth/me can tell the frontend this is the no-login local owner.
    assert result is not None
    assert result["id"] == "local"
    assert result["auth_mode"] == "local"


def test_user_from_connection_none_without_local_owner() -> None:
    assert user_from_connection(_Req(None, session_token=None)) is None


def test_user_from_connection_ignores_local_owner_when_token_set() -> None:
    owner = {"id": "local", "role": "admin", "is_active": 1}
    assert user_from_connection(_Req(owner, session_token="s3cret")) is None


# ---------------------------------------------------------------------------
# End-to-end through the FastAPI app
# ---------------------------------------------------------------------------


def test_local_owner_bypasses_admin_gate() -> None:
    """No token, no cookie: the admin-only /api/admin/users is reachable."""
    with TestClient(create_app(session_token=None, local_owner=True)) as client:
        response = client.get("/api/admin/users")
    assert response.status_code == 200


def test_without_local_owner_admin_gate_requires_login() -> None:
    """local_owner off → /api/admin/users is 401 without credentials."""
    with TestClient(create_app(session_token=None, local_owner=False)) as client:
        response = client.get("/api/admin/users")
    assert response.status_code == 401


def test_local_owner_keeps_token_gate_when_token_set() -> None:
    """A configured token wins over local owner: no token → 401, correct → 200."""
    with TestClient(create_app(session_token="s3cret", local_owner=True)) as client:
        assert client.get("/api/config/").status_code == 401
        assert (
            client.get("/api/config/", headers={"Authorization": "Bearer s3cret"}).status_code
            == 200
        )
