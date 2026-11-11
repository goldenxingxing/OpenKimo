"""Archive the full pre-compaction context so compaction stays reversible.

OpenKimo's ``SimpleCompaction`` is lossy: it keeps a summary plus the recent
tail, and the mid-history that gets summarised away is otherwise gone (the
``archive_compaction`` memory hook only persists the *summary*, into
``recent.jsonl``). This module writes the complete pre-compaction history to
``<session>/archive/`` before the context is cleared, and the
``ContextRetrieve`` tool lets the model read the exact original text back when
the summary is not enough.

The idea mirrors Headroom's CCR (compress for the model, keep the original,
retrieve on demand), but is implemented against the session's own archive
directory rather than an external proxy. Lossless by construction: each message
is serialised with the same ``model_dump_json`` the context file uses, so a
later read reproduces the exact original content.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from kosong.message import Message

from kimo_cli.utils.logging import logger

ARCHIVE_DIRNAME = "archive"


class CompactionArchiver:
    """Writes the full pre-compaction history into ``<session>/archive/``."""

    def __init__(self, archive_dir: Path) -> None:
        self._dir = archive_dir

    @property
    def dir(self) -> Path:
        return self._dir

    def archive(self, history: Sequence[Message]) -> Path:
        """Persist every pre-compaction message as JSONL; return the file path.

        Serialisation is synchronous and cheap enough for the sizes involved;
        callers on the event loop should wrap this in ``asyncio.to_thread``.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"compaction-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.jsonl"
        payload = "".join(m.model_dump_json(exclude_none=True) + "\n" for m in history)
        path.write_text(payload, encoding="utf-8")
        logger.info(
            "Archived {n} pre-compaction messages ({bytes} bytes) to {path}",
            n=len(history),
            bytes=len(payload.encode("utf-8")),
            path=path,
        )
        return path

    @staticmethod
    def reference(path: Path, *, n_messages: int) -> str:
        """Note appended to the compaction summary pointing at the archive.

        Wrapped in a ``<system>`` part by the caller so the cross-session memory
        extractor (which skips ``<system>`` parts) does not carry it into memory.
        """
        return (
            f"\n\n[Full pre-compaction context archived to: {path} "
            f"({n_messages} messages). If the summary above lacks a detail you "
            f"need, use the ContextRetrieve tool to read the exact original text.]"
        )
