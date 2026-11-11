"""Filesystem checkpoints: git snapshots keyed by conversation checkpoint id.

A D-Mail revert used to restore only the conversation; the workspace files
stayed at whatever state the agent had left them (denwarenji.py TODO). The
FsCheckpointer snapshots the workspace at every checkpoint and restores it on
revert — adapted from QwenPaw's checkpoints module, with two safety rails:
it only ever manages a repo it auto-created, and every failure is fail-open.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kimo_cli.soul.fs_checkpoint import FsCheckpointer, workspace_from_env


async def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


@pytest.mark.asyncio
async def test_snapshot_and_restore_round_trip(tmp_path: Path) -> None:
    cp = FsCheckpointer(tmp_path)
    await _write(tmp_path / "a.txt", "v1")

    await cp.snapshot(0)
    await _write(tmp_path / "a.txt", "v2")
    await _write(tmp_path / "b.txt", "created later")
    await cp.snapshot(1)

    restored = await cp.restore(0)
    assert restored
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1"
    # Untracked-at-checkpoint-0 files are removed in a managed repo.
    assert not (tmp_path / "b.txt").exists()


@pytest.mark.asyncio
async def test_restore_to_second_checkpoint(tmp_path: Path) -> None:
    cp = FsCheckpointer(tmp_path)
    await _write(tmp_path / "a.txt", "v1")
    await cp.snapshot(0)
    await _write(tmp_path / "a.txt", "v2")
    await cp.snapshot(1)
    await _write(tmp_path / "a.txt", "v3")
    await cp.snapshot(2)

    assert await cp.restore(1)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v2"


@pytest.mark.asyncio
async def test_restore_prunes_later_snapshots(tmp_path: Path) -> None:
    cp = FsCheckpointer(tmp_path)
    await cp.snapshot(0)
    await cp.snapshot(1)
    await cp.snapshot(2)

    await cp.restore(1)
    # Checkpoint ids continue after a revert, so anything past the target is
    # unreachable — the map must not serve it again.
    assert not await cp.restore(2)
    assert await cp.restore(1)


@pytest.mark.asyncio
async def test_restore_without_snapshot_is_noop(tmp_path: Path) -> None:
    cp = FsCheckpointer(tmp_path)
    assert not await cp.restore(0)


@pytest.mark.asyncio
async def test_existing_repo_is_never_touched(tmp_path: Path) -> None:
    # A workspace that is already a git repo disables the checkpointer
    # instead of mixing checkpoint commits into user history.
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "user.txt").write_text("user data", encoding="utf-8")
    cp = FsCheckpointer(tmp_path)

    await cp.snapshot(0)
    # Nothing committed into the user's repo.
    heads = tmp_path / ".git" / "refs" / "heads"
    assert not heads.exists() or not any(heads.iterdir())
    assert not await cp.restore(0)  # no snapshot was taken
    assert (tmp_path / "user.txt").read_text(encoding="utf-8") == "user data"


@pytest.mark.asyncio
async def test_git_failure_is_fail_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cp = FsCheckpointer(tmp_path)

    class _FailedProc:
        returncode = 1
        stdout = b""
        stderr = b"git broken"

    async def _broken(*_args: object, **_kwargs: object) -> _FailedProc:
        return _FailedProc()

    monkeypatch.setattr(cp, "_run_git", _broken)
    await cp.snapshot(0)  # must not raise
    assert not await cp.restore(0)

    # And a later probe after the failure still degrades gracefully.
    await cp.snapshot(1)


def test_workspace_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIMO_WORK_DIR", str(tmp_path))
    assert workspace_from_env() == tmp_path
    monkeypatch.delenv("KIMO_WORK_DIR")
    assert workspace_from_env() == Path.cwd()
