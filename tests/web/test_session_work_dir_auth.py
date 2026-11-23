"""Application-level work-directory authorization for multi-user web mode."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest
from fastapi import HTTPException

from kimo_cli.session_state import load_session_state
from kimo_cli.web.api import sessions as sessions_api
from kimo_cli.web.api.sessions import CreateSessionRequest

if TYPE_CHECKING:
    from fastapi import Request

    from kimo_cli.web.models import Session


def _request() -> Request:
    return cast("Request", SimpleNamespace(cookies={}, headers={}))


@pytest.fixture
def isolated_share_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    share_dir = tmp_path / "share"

    def get_share_dir() -> Path:
        share_dir.mkdir(parents=True, exist_ok=True)
        return share_dir

    monkeypatch.setattr("kimo_cli.share.get_share_dir", get_share_dir)
    monkeypatch.setattr("kimo_cli.metadata.get_share_dir", get_share_dir)
    monkeypatch.setattr(sessions_api, "get_share_dir", get_share_dir)
    return share_dir


@pytest.mark.anyio
async def test_multi_user_create_rejects_a_service_readable_directory_outside_roots(
    monkeypatch: pytest.MonkeyPatch,
    isolated_share_dir: Path,
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("KIMO_WEB_ALLOWED_WORK_DIRS", str(allowed))
    monkeypatch.setattr(
        "kimo_cli.web.user_auth.get_current_user",
        lambda _request: {"id": "user-1", "role": "user"},
    )

    with pytest.raises(HTTPException) as exc_info:
        await sessions_api.create_session(_request(), CreateSessionRequest(work_dir=str(outside)))

    assert exc_info.value.status_code == 403
    legacy_session = cast("Session", SimpleNamespace(owner_id="user-1", work_dir=str(outside)))
    assert not sessions_api.may_access_session(legacy_session, {"id": "user-1", "role": "user"})
    monkeypatch.setattr(sessions_api, "load_session_by_id", lambda _session_id: legacy_session)
    with pytest.raises(HTTPException) as file_error:
        await sessions_api.get_session_file(uuid4(), "secret.txt", _request())
    assert file_error.value.status_code == 404


@pytest.mark.anyio
async def test_multi_user_create_allows_a_configured_root(
    monkeypatch: pytest.MonkeyPatch,
    isolated_share_dir: Path,
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    work_dir = allowed / "project"
    work_dir.mkdir(parents=True)
    monkeypatch.setenv("KIMO_WEB_ALLOWED_WORK_DIRS", str(allowed))
    monkeypatch.setattr(
        "kimo_cli.web.user_auth.get_current_user",
        lambda _request: {"id": "user-1", "role": "user"},
    )

    result = await sessions_api.create_session(
        _request(), CreateSessionRequest(work_dir=str(work_dir))
    )

    assert result.work_dir == str(work_dir.resolve())
    assert result.session_dir is not None
    assert load_session_state(Path(result.session_dir)).owner_id == "user-1"


@pytest.mark.anyio
async def test_local_owner_keeps_arbitrary_directory_selection(
    monkeypatch: pytest.MonkeyPatch,
    isolated_share_dir: Path,
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "local-choice"
    work_dir.mkdir()
    monkeypatch.setenv("KIMO_WEB_ALLOWED_WORK_DIRS", str(tmp_path / "some-other-root"))
    local_user: dict[str, Any] = {"id": "local", "role": "user", "auth_mode": "local"}
    monkeypatch.setattr("kimo_cli.web.user_auth.get_current_user", lambda _request: local_user)

    result = await sessions_api.create_session(
        _request(), CreateSessionRequest(work_dir=str(work_dir))
    )

    assert result.work_dir == str(work_dir.resolve())


@pytest.mark.anyio
async def test_multi_user_default_workspace_is_per_user_not_service_home(
    monkeypatch: pytest.MonkeyPatch,
    isolated_share_dir: Path,
) -> None:
    monkeypatch.delenv("KIMO_DEFAULT_WORK_DIR", raising=False)
    monkeypatch.delenv("KIMO_WEB_ALLOWED_WORK_DIRS", raising=False)
    monkeypatch.setattr(
        "kimo_cli.web.user_auth.get_current_user",
        lambda _request: {"id": "user-1", "role": "user"},
    )

    result = await sessions_api.create_session(_request())

    assert result.work_dir == str((isolated_share_dir / "workspaces" / "user-1").resolve())
