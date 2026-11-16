from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_json_write(data: Any, path: Path, *, fsync: bool = True) -> None:
    """Write JSON data to a file atomically using tmp-file + os.replace.

    This prevents data corruption if the process crashes mid-write: either the
    old file is kept intact or the new file is fully committed.

    ``fsync=False`` skips the durability fsync. Callers that write in a tight
    loop (e.g. background-task heartbeats) use this so a slow/stalled disk
    cannot freeze their event loop; the tmp-file + os.replace still guarantees
    a consistent file for any concurrent reader.
    """
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        # fdopen takes ownership of the descriptor only once it succeeds. If it
        # raises, the `with` is never entered and the raw fd stays open for the
        # life of the process — and this is the session-state write path, which
        # runs on every state change.
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with handle as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
