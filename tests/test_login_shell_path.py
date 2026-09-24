"""A Finder-launched app must still find the tools the user installed.

launchd hands a GUI app /usr/bin:/bin:/usr/sbin:/sbin. The server and the
agent's shell tool inherit that, so Homebrew's node and npm — which a skill
like email-manager runs — were "command not found" inside the app.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

from app_main import userenv  # noqa: E402


def _paths(tmp_path: Path) -> SimpleNamespace:
    runtime = tmp_path / "runtimes" / "cpython-3.12"
    return SimpleNamespace(
        userbase=tmp_path / "userbase",
        userbase_bin=tmp_path / "userbase" / "bin",
        pip_conf=tmp_path / "pip.conf",
        bundled_python_root=runtime,
        bundled_python_bin=runtime / "bin",
    )


def test_login_shell_entries_follow_the_bundled_interpreter(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(userenv, "login_shell_path", lambda: "/opt/homebrew/bin:/usr/bin")

    entries = userenv.env_overlay(_paths(tmp_path))["PATH"].split(":")

    assert entries == [
        str(tmp_path / "userbase" / "bin"),
        str(tmp_path / "runtimes" / "cpython-3.12" / "bin"),
        "/usr/bin",
        "/bin",
        "/opt/homebrew/bin",
    ]


def test_output_a_profile_prints_does_not_leak_into_path(monkeypatch) -> None:
    userenv.login_shell_path.cache_clear()
    monkeypatch.setattr(userenv.sys, "platform", "darwin")

    def fake_run(*_args, **_kwargs):
        stdout = (
            "Welcome back!\n"
            f"{userenv._PATH_BEGIN}/opt/homebrew/bin:/usr/bin{userenv._PATH_END}"
        )
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(userenv.subprocess, "run", fake_run)
    try:
        assert userenv.login_shell_path() == "/opt/homebrew/bin:/usr/bin"
    finally:
        userenv.login_shell_path.cache_clear()


@pytest.mark.parametrize(
    "outcome",
    [subprocess.TimeoutExpired("zsh", 5), OSError("no such shell"), "no markers"],
)
def test_a_shell_that_fails_leaves_path_alone(monkeypatch, outcome) -> None:
    userenv.login_shell_path.cache_clear()
    monkeypatch.setattr(userenv.sys, "platform", "darwin")

    def fake_run(*_args, **_kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess([], 1, stdout=outcome, stderr="")

    monkeypatch.setattr(userenv.subprocess, "run", fake_run)
    try:
        assert userenv.login_shell_path() == ""
    finally:
        userenv.login_shell_path.cache_clear()


def test_windows_does_not_ask_a_login_shell(monkeypatch) -> None:
    userenv.login_shell_path.cache_clear()
    monkeypatch.setattr(userenv.sys, "platform", "win32")
    monkeypatch.setattr(
        userenv.subprocess, "run", lambda *a, **k: pytest.fail("no shell on Windows")
    )
    try:
        assert userenv.login_shell_path() == ""
    finally:
        userenv.login_shell_path.cache_clear()
