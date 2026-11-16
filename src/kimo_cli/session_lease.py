"""Epoch lease against double writers on the same session directory.

Adapted from jcode's ``session_lease.rs``. A monotonic epoch counter lives in
``lease.json`` inside the session directory. A worker claims the lease once at
startup — incrementing the counter and remembering its value — and re-checks
it before every session-file write (wire transcript, context history). If the
on-disk epoch has moved past ours, another worker has taken over the session
(the gateway restarted and spawned a fresh worker while this one was still
alive), and this worker must stop writing: its in-memory view is stale, and
appending would fork the session history.

Unlike jcode, the epoch does NOT live in ``state.json``: that file is already
read-modify-written concurrently by the web API (titles, plan mode, todos),
and a last-writer-wins race there could silently clobber the counter. A
dedicated file with one writer (the current lease holder) keeps the invariant
trivial. Old sessions without ``lease.json`` start at epoch 0 on first claim.

Zero cost when no takeover ever happens: one small file read per write, and
no coordination when the epoch matches.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from kimo_cli.utils.io import atomic_json_write
from kimo_cli.utils.logging import logger

LEASE_FILE_NAME = "lease.json"

# msvcrt locks are process-associated on Windows; serialize local callers too.
_windows_lock = threading.Lock()


class LeaseLostError(RuntimeError):
    """Raised when a write is attempted after another worker claimed the lease."""


@dataclass(frozen=True)
class SessionLease:
    """A claimed lease: the directory it guards and the epoch we hold."""

    session_dir: Path
    epoch: int

    @property
    def path(self) -> Path:
        return self.session_dir / LEASE_FILE_NAME

    def verify(self) -> None:
        """Raise :class:`LeaseLostError` if we are no longer the latest holder."""
        try:
            disk_epoch = _read_epoch(self.path)
        except (OSError, ValueError) as e:
            # Cannot read the lease file: fail closed. A transient FS error or
            # a corrupted file is cheaper than silently forking a session.
            raise LeaseLostError(
                f"Cannot read lease file {self.path}: {e}; refusing to write"
            ) from e
        if disk_epoch != self.epoch:
            raise LeaseLostError(
                f"Session lease lost: holding epoch {self.epoch}, "
                f"disk has {disk_epoch} ({self.path}); another worker owns this session"
            )


def _read_epoch(path: Path) -> int:
    """Read the on-disk epoch; a missing file means epoch 0 (never claimed)."""
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid lease payload in {path}: {data!r}")
    epoch = data.get("epoch", 0)
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise ValueError(f"Invalid lease epoch in {path}: {epoch!r}")
    return epoch


def claim_session_lease(session_dir: Path) -> SessionLease:
    """Increment the on-disk epoch and return the caller's lease.

    Called once per worker startup. Any previously-running worker holding an
    older epoch fails its next ``verify`` and stops writing.
    """
    path = session_dir / LEASE_FILE_NAME
    with _claim_lock(session_dir):
        disk_epoch = _read_epoch(path)
        new_epoch = disk_epoch + 1
        atomic_json_write(
            {"epoch": new_epoch, "claimed_at": time.time()},
            path,
        )
    lease = SessionLease(session_dir=session_dir, epoch=new_epoch)
    logger.info(
        "Claimed session lease epoch {epoch} for {path}",
        epoch=new_epoch,
        path=session_dir,
    )
    return lease


@contextlib.contextmanager
def _claim_lock(session_dir: Path) -> Iterator[None]:
    """Serialize the epoch read-modify-write across threads and processes."""
    session_dir.mkdir(parents=True, exist_ok=True)
    lock_path = session_dir / f"{LEASE_FILE_NAME}.lock"
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            with _windows_lock:
                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
