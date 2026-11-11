"""A session's "last updated" time is when it was last used, not last opened.

Opening a session can rewrite its context file in place, so the sidebar used to
show a session untouched for days as updated moments ago.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from kimo_cli.web.store.sessions import session_last_activity


def _write(path: Path, mtime: int) -> None:
    path.write_text("{}\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_rewriting_the_context_file_does_not_count_as_activity(tmp_path: Path) -> None:
    context_file = tmp_path / "context.jsonl"
    _write(tmp_path / "wire.jsonl", 1_000_000)
    _write(context_file, 2_000_000)

    assert session_last_activity(tmp_path, context_file) == datetime.fromtimestamp(
        1_000_000, tz=UTC
    )


def test_a_session_without_a_wire_file_falls_back_to_the_context_file(tmp_path: Path) -> None:
    context_file = tmp_path / "context.jsonl"
    _write(context_file, 2_000_000)

    assert session_last_activity(tmp_path, context_file) == datetime.fromtimestamp(
        2_000_000, tz=UTC
    )
