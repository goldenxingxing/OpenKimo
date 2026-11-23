"""Security boundaries of the public login and session logout endpoints."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient

from kimo_cli.web.api import auth_user
from kimo_cli.web.db.crud import create_user_session, get_user_session
from kimo_cli.web.db.database import db_session, init_db
from kimo_cli.web.session_policy import COOKIE_NAME, build_cookie_header

_TEST_ADMIN_PASSWORD = "test-admin-password"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A small auth-only app on a disposable database, with clean auth env."""
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))
    monkeypatch.setenv("KIMO_INITIAL_ADMIN_PASSWORD", _TEST_ADMIN_PASSWORD)
    monkeypatch.delenv("KIMO_WEB_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("KIMO_WEB_COOKIE_SECURE", raising=False)
    init_db()

    app = FastAPI()
    app.include_router(auth_user.router)
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient) -> tuple[str, dict[str, str]]:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": _TEST_ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    token = client.cookies.get(COOKIE_NAME)
    assert token
    return token, dict(response.headers)


def test_logout_failure_never_logs_the_replayable_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, _ = _login(client)
    warnings: list[str] = []

    def fail_delete(_db, _token: str) -> None:
        raise RuntimeError(f"database error while deleting {token}")

    monkeypatch.setattr(auth_user, "delete_user_session", fail_delete)
    monkeypatch.setattr(
        auth_user.logger,
        "warning",
        lambda message, **kwargs: warnings.append(f"{message} {kwargs!r}"),
    )

    response = client.post("/api/auth/logout")

    assert response.status_code == 200
    assert warnings
    assert token not in "\n".join(warnings)


def test_bearer_only_logout_revokes_the_database_session(client: TestClient) -> None:
    token, _ = _login(client)
    client.cookies.clear()

    response = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert (
        client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    )


def test_cookie_wins_when_logout_carries_both_transports(client: TestClient) -> None:
    cookie_token, _ = _login(client)
    with db_session() as db:
        user = get_user_session(db, cookie_token)
        assert user is not None
        bearer_token = create_user_session(db, user["id"], expires_in_seconds=3600)

    response = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {bearer_token}"})

    assert response.status_code == 200
    with db_session() as db:
        assert get_user_session(db, cookie_token) is None
        assert get_user_session(db, bearer_token) is not None


def test_https_deployment_marks_login_renewal_and_logout_cookies_secure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIMO_WEB_COOKIE_SECURE", "true")

    token, headers = _login(client)

    assert "Secure" in headers["set-cookie"]
    assert "; Secure" in build_cookie_header(token, 3600)
    response = client.post("/api/auth/logout")
    assert "Secure" in response.headers["set-cookie"]


def test_local_http_cookie_ignores_spoofed_forwarded_https_headers(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": _TEST_ADMIN_PASSWORD},
        headers={"X-Forwarded-Proto": "https", "Forwarded": "proto=https"},
    )

    assert response.status_code == 200
    assert "Secure" not in response.headers["set-cookie"]
    assert "; Secure" not in build_cookie_header("token", 3600)


def test_login_returns_429_before_starting_an_excess_kdf(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FullSlots:
        def acquire(self, *, blocking: bool) -> bool:
            assert not blocking
            return False

        def release(self) -> None:
            pytest.fail("an unacquired KDF slot must not be released")

    monkeypatch.setattr(auth_user, "_LOGIN_KDF_SLOTS", FullSlots())
    monkeypatch.setattr(
        auth_user,
        "verify_password",
        lambda *_args: pytest.fail("429 path must not start password verification"),
    )

    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": _TEST_ADMIN_PASSWORD},
    )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"


def test_second_kdf_is_rejected_while_the_only_slot_is_busy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    del client  # The fixture still supplies its isolated, seeded database.
    monkeypatch.setattr(auth_user, "_LOGIN_KDF_SLOTS", threading.BoundedSemaphore(1))
    kdf_started = threading.Event()
    finish_kdf = threading.Event()

    def slow_verify(_password: str, _password_hash: str) -> bool:
        kdf_started.set()
        assert finish_kdf.wait(timeout=5)
        return True

    monkeypatch.setattr(auth_user, "verify_password", slow_verify)
    first_result: list[auth_user.UserResponse] = []
    first_errors: list[BaseException] = []

    def first_login() -> None:
        try:
            first_result.append(
                auth_user.login(
                    auth_user.LoginRequest(username="admin", password=_TEST_ADMIN_PASSWORD),
                    Response(),
                )
            )
        except BaseException as exc:  # Surface worker-thread failures in the assertion below.
            first_errors.append(exc)

    thread = threading.Thread(target=first_login)
    thread.start()
    try:
        assert kdf_started.wait(timeout=5)
        with pytest.raises(HTTPException) as exc_info:
            auth_user.login(
                auth_user.LoginRequest(username="admin", password=_TEST_ADMIN_PASSWORD),
                Response(),
            )
        assert exc_info.value.status_code == 429
    finally:
        finish_kdf.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert not first_errors
    assert len(first_result) == 1
