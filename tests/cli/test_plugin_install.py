"""CLI plugin install: a failed clone must not leak the scratch directory."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import typer

from kimo_cli.cli import plugin as plugin_cli

_real_run = subprocess.run


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect `tempfile.mkdtemp` into tmp_path so leftovers are observable."""
    created: list[Path] = []

    def _mkdtemp(prefix: str = "") -> str:
        d = tmp_path / f"{prefix}{len(created)}"
        d.mkdir()
        created.append(d)
        return str(d)

    # `tempfile` is imported inside the function under test, so patch the module.
    monkeypatch.setattr(tempfile, "mkdtemp", _mkdtemp)
    return tmp_path


def _fail_git(monkeypatch: pytest.MonkeyPatch, exc_factory: Callable[[], BaseException]) -> None:
    """Make `git clone` fail without breaking every other subprocess call.

    The sandbox helper used by `shutil.rmtree` shells out through
    `subprocess.run` too, so a blanket patch would break the cleanup this test
    is trying to observe.
    """

    def _run(cmd: Any = None, *args: Any, **kwargs: Any) -> Any:
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
            raise exc_factory()
        return _real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _run)


def test_git_clone_timeout_cleans_up_and_exits(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`subprocess.run(timeout=300)` raises TimeoutExpired, which used to escape.

    The exception left the `kimo-plugin-*` clone directory behind, and the
    caller's `finally` could not clean it up: `_resolve_source` raised before
    returning the tmp path.
    """

    _fail_git(
        monkeypatch,
        lambda: subprocess.TimeoutExpired(cmd="git clone", timeout=300),
    )

    with pytest.raises(typer.Exit):
        plugin_cli._resolve_source("https://github.com/example/plugin")

    assert list(scratch.iterdir()) == []


def test_git_missing_cleans_up_and_exits(scratch: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing `git` binary raises FileNotFoundError from the same call."""

    _fail_git(monkeypatch, lambda: FileNotFoundError(2, "No such file or directory", "git"))

    with pytest.raises(typer.Exit):
        plugin_cli._resolve_source("https://github.com/example/plugin")

    assert list(scratch.iterdir()) == []
