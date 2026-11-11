"""Filesystem checkpoints: git snapshots of the workspace at each checkpoint.

Closes the TODO in ``denwarenji.py`` — a D-Mail revert now restores the
workspace to the state it had at the target checkpoint, not just the
conversation. Adapted from QwenPaw's ``checkpoints`` module (workspace git
snapshots + transactional restore), scaled down to OpenKimo's shape.

Conservative by design:

- the checkpointer only manages a repo it auto-created in the workspace. A
  pre-existing ``.git`` is left completely alone (no checkpoint commits in
  the user's history) and filesystem checkpointing disables itself;
- every failure is fail-open and logged: filesystem checkpointing must never
  break the agent loop. A failed snapshot degrades to today's behavior
  (conversation-only revert).

Snapshots are keyed by conversation checkpoint id, which the context numbers
in increasing order and continues after a revert (the rebuilt file replays
the checkpoint markers), so after a revert to checkpoint N only entries > N
are pruned.

The workspace is ``KIMO_WORK_DIR`` when set (the sandbox contract) and the
process cwd otherwise (terminal CLI on the host).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from kimo_cli.utils.logging import logger

_GIT_IDENTITY = [
    "-c",
    "user.name=OpenKimo Checkpointer",
    "-c",
    "user.email=checkpointer@openkimo.local",
    "-c",
    "commit.gpgsign=false",
]

_SNAPSHOT_TAG = "kimo-checkpoint"


class FsCheckpointer:
    """Async git snapshot/restore of the workspace keyed by checkpoint id."""

    def __init__(self, workspace: Path):
        self._workspace = workspace
        self._snapshots: dict[int, str] = {}
        self._usable: bool | None = None
        """Lazy tri-state: None = not probed yet; False = disabled."""
        self._managed_repo = False

    # ── public API ───────────────────────────────────────────────────────────

    async def snapshot(self, checkpoint_id: int) -> None:
        """Commit the workspace and map `checkpoint_id` to the resulting HEAD.

        No-op (logged) when disabled, when nothing changed and no HEAD exists
        yet, or on any git failure.
        """
        if not await self._ensure_usable():
            return
        try:
            await self._git("add", "-A")
            # --allow-empty: every checkpoint gets its own commit, so an
            # unchanged (or brand-new empty) workspace still maps to a valid
            # restore point instead of silently taking the previous HEAD.
            await self._git("commit", "--allow-empty", "-m", f"{_SNAPSHOT_TAG} {checkpoint_id}")
            head = await self._git("rev-parse", "HEAD")
        except Exception as e:
            # A snapshot that cannot be taken must not break the loop.
            logger.warning(
                "Filesystem snapshot for checkpoint {id} failed: {error}",
                id=checkpoint_id,
                error=e,
            )
            return
        self._snapshots[checkpoint_id] = head
        logger.debug(
            "Filesystem snapshot for checkpoint {id}: {head}",
            id=checkpoint_id,
            head=head[:12],
        )

    async def restore(self, checkpoint_id: int) -> bool:
        """Reset the workspace to its snapshot for `checkpoint_id`.

        Returns True when a restore was performed. Untracked files are only
        removed in a checkpointer-managed repo; in no other repo do we ever
        operate.
        """
        head = self._snapshots.get(checkpoint_id)
        if head is None:
            logger.warning(
                "No filesystem snapshot for checkpoint {id}; files not restored",
                id=checkpoint_id,
            )
            return False
        try:
            await self._git("reset", "--hard", head)
            if self._managed_repo:
                await self._git("clean", "-fd")
        except Exception as e:
            logger.warning(
                "Filesystem restore to checkpoint {id} failed: {error}",
                id=checkpoint_id,
                error=e,
            )
            return False
        # Checkpoint ids continue after a revert; anything past the target is
        # gone from the conversation and its snapshots are unreachable.
        for stale in [cid for cid in self._snapshots if cid > checkpoint_id]:
            del self._snapshots[stale]
        logger.info(
            "Workspace restored to snapshot of checkpoint {id} ({head})",
            id=checkpoint_id,
            head=head[:12],
        )
        return True

    # ── internals ────────────────────────────────────────────────────────────

    async def _ensure_usable(self) -> bool:
        """One-time probe: git present, and the workspace is ours to manage."""
        if self._usable is not None:
            return self._usable
        self._usable = False
        try:
            rc, _, _ = await self._run_git("version")
            if rc != 0:
                logger.warning("git not available; filesystem checkpoints disabled")
                return False
            # Directly check for .git existence — `rev-parse
            # --is-inside-work-tree` returns non-zero on a corrupted .git
            # (empty dir, truncated index, wrong ownership, broken symlink)
            # and would fall through to `git init`, which succeeds even on
            # an existing .git and marks the workspace as managed. That
            # combination lets `restore()` later run `git clean -fd` on
            # the user's repo, deleting untracked files. Existence-based
            # detection is the promise in the module docstring.
            git_marker = self._workspace / ".git"
            if git_marker.exists():
                logger.info(
                    "Workspace {path} already has a .git; filesystem "
                    "checkpoints disabled (never touching user history)",
                    path=self._workspace,
                )
                return False
            rc, out, _ = await self._run_git("rev-parse", "--is-inside-work-tree")
            if rc == 0 and out.strip() == b"true":
                # Nested inside a parent repo — treat as user-managed.
                logger.info(
                    "Workspace {path} is inside a git repo; filesystem "
                    "checkpoints disabled (never touching user history)",
                    path=self._workspace,
                )
                return False
            await self._git("init")
            self._managed_repo = True
        except Exception as e:
            logger.warning(
                "Filesystem checkpointing unavailable in {path}: {error}",
                path=self._workspace,
                error=e,
            )
            return False
        self._usable = True
        logger.info(
            "Filesystem checkpointing enabled in managed repo {path}",
            path=self._workspace,
        )
        return True

    async def _git(self, *args: str) -> str:
        rc, out, err = await self._run_git(*_GIT_IDENTITY, *args)
        if rc != 0:
            raise RuntimeError(
                f"git {' '.join(args[:2])} failed ({rc}): {err.decode(errors='replace')[:300]}"
            )
        return out.decode(errors="replace").strip()

    async def _run_git(self, *args: str) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=self._workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode if proc.returncode is not None else -1, out, err


def workspace_from_env() -> Path:
    """The workspace the checkpointer should snapshot."""
    return Path(os.environ.get("KIMO_WORK_DIR", str(Path.cwd())))
