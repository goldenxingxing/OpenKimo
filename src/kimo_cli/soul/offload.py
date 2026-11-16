"""Offload oversized tool output to disk, leaving a reference in the message.

Tool output is truncated to fit the context budget (50k chars for builtin
tools via ``ToolResultBuilder``, 100k for MCP results). Truncation used to
mean the rest was gone for good; the offloader writes the full text to the
session's ``offload/`` directory and the truncated message carries a path the
model can Read back when it actually needs the details (QwenPaw's offloader
takes the same approach).
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from kimo_cli.utils.logging import logger

#: The offloader for the tool call currently being handled. ``KimoToolset.handle``
#: sets it per call so builtin tools get offloading without each ``ToolResultBuilder``
#: construction site needing a new parameter.
current_offloader: ContextVar[ToolOutputOffloader | None] = ContextVar(
    "current_offloader", default=None
)


class ToolOutputOffloader:
    """Writes oversized tool output into ``<session>/offload/`` on demand."""

    def __init__(self, offload_dir: Path) -> None:
        self._dir = offload_dir

    @property
    def dir(self) -> Path:
        return self._dir

    def _new_path(self, tool_name: str) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", tool_name) or "tool"
        return self._dir / f"{datetime.now():%Y%m%d-%H%M%S}-{safe_name}-{uuid4().hex[:8]}.txt"

    def offload_text(self, text: str, *, tool_name: str) -> Path:
        """Persist ``text`` in one shot and return the path it was written to."""
        path = self._new_path(tool_name)
        path.write_text(text, encoding="utf-8")
        logger.info(
            "Offloaded {chars} chars of {tool} output to {path}",
            chars=len(text),
            tool=tool_name,
            path=path,
        )
        return path

    def new_sink(self, *, tool_name: str) -> OffloadSink:
        """Open a streaming sink for output that overflows incrementally
        (e.g. ``ToolResultBuilder.write`` hitting its budget mid-stream)."""
        return OffloadSink(self._new_path(tool_name))

    @staticmethod
    def reference(path: Path, *, total_chars: int | None = None) -> str:
        """The trailer appended to truncated output pointing at the full text."""
        size = f" ({total_chars} chars)" if total_chars is not None else ""
        return (
            f"\n\n[Full output saved to: {path}{size}. "
            f"Read the file if you need the truncated parts.]"
        )


class OffloadSink:
    """Append-only file sink for streaming overflow. ``close`` removes the
    file when nothing was ever written, so conditional offloading stays
    side-effect free."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None
        self._written = 0

    def write(self, text: str) -> None:
        if not text:
            return
        if self._handle is None:
            # The sink deliberately holds the handle open across writes until
            # close(); a per-write context manager would reopen per chunk.
            self._handle = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
        self._handle.write(text)
        self._written += len(text)

    @property
    def written(self) -> int:
        return self._written

    def close(self) -> Path | None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self._written == 0:
            self.path.unlink(missing_ok=True)
            return None
        return self.path
