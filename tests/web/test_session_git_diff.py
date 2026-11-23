"""Real git status, unborn-index, and command-failure regression tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from kimo_cli.web.api import sessions as sessions_api

if TYPE_CHECKING:
    from fastapi import Request


def _request() -> Request:
    return cast("Request", SimpleNamespace(cookies={}, headers={}))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")


@pytest.fixture
def session_lookup(monkeypatch: pytest.MonkeyPatch):
    def install(repo: Path) -> None:
        session = SimpleNamespace(
            owner_id=None,
            work_dir=str(repo),
            kimo_cli_session=SimpleNamespace(work_dir=repo),
        )
        monkeypatch.setattr(sessions_api, "load_session_by_id", lambda _session_id: session)
        monkeypatch.setattr("kimo_cli.web.user_auth.get_current_user", lambda _request: None)

    return install


@pytest.mark.anyio
async def test_unborn_head_includes_staged_files_and_numstat(
    tmp_path: Path, session_lookup
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "staged.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    session_lookup(repo)

    result = await sessions_api.get_session_git_diff(uuid4(), _request())

    assert result.error is None
    assert result.has_changes is True
    assert result.total_additions == 2
    assert result.total_deletions == 0
    assert [(f.path, f.status, f.additions, f.deletions) for f in result.files] == [
        ("staged.txt", "added", 2, 0)
    ]


@pytest.mark.anyio
async def test_tracked_add_only_and_delete_only_edits_remain_modified(
    tmp_path: Path, session_lookup
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "add-lines.txt").write_text("base\n", encoding="utf-8")
    (repo / "delete-lines.txt").write_text("base\nremove\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "initial")
    (repo / "add-lines.txt").write_text("base\nadded\n", encoding="utf-8")
    (repo / "delete-lines.txt").write_text("base\n", encoding="utf-8")
    session_lookup(repo)

    result = await sessions_api.get_session_git_diff(uuid4(), _request())

    assert result.error is None
    files = {f.path: f for f in result.files}
    assert (files["add-lines.txt"].status, files["add-lines.txt"].additions) == (
        "modified",
        1,
    )
    assert (
        files["delete-lines.txt"].status,
        files["delete-lines.txt"].deletions,
    ) == ("modified", 1)
    assert result.total_additions == 1
    assert result.total_deletions == 1


class _FakeGitProcess:
    def __init__(self, args: tuple[str, ...], failing_command: str) -> None:
        command = args[1]
        self.returncode = 1 if command == failing_command else 0
        self._stdout = b""
        self._stderr = f"fatal: simulated {command} failure".encode() if self.returncode else b""

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


@pytest.mark.anyio
@pytest.mark.parametrize("failing_command", ["diff", "ls-files"])
async def test_git_nonzero_exit_and_stderr_are_returned_as_an_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_lookup,
    failing_command: str,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    session_lookup(repo)

    async def create_subprocess_exec(*args: str, **_kwargs: object) -> _FakeGitProcess:
        return _FakeGitProcess(args, failing_command)

    monkeypatch.setattr(sessions_api.asyncio, "create_subprocess_exec", create_subprocess_exec)

    result = await sessions_api.get_session_git_diff(uuid4(), _request())

    assert result.has_changes is False
    assert result.files == []
    assert result.error is not None
    assert f"git {failing_command} failed" in result.error
    assert f"simulated {failing_command} failure" in result.error
