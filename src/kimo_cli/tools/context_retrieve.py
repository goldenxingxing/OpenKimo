"""Read back the exact original text of context that was compacted away.

Context compaction (``SimpleCompaction``) keeps a summary plus the recent tail;
the full pre-compaction history is archived under the session's ``archive/``
directory by ``CompactionArchiver``. This tool lets the model retrieve the exact
original messages when the summary lacks a detail it needs.

Unlike an opaque external cache, a missing/invalid archive returns an explicit
error rather than silently returning nothing — the lesson from Headroom's
issue #4111, where a silent retrieval miss was indistinguishable from a real
"no data".
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal, override

from kosong.message import Message
from kosong.tooling import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field, ValidationError

from kimo_cli.soul.agent import Runtime
from kimo_cli.soul.compaction_archive import ARCHIVE_DIRNAME
from kimo_cli.utils.logging import logger

DEFAULT_MAX_CHARS = 20_000


class Params(BaseModel):
    action: Literal["read", "list"] = Field(
        default="read",
        description=(
            "'read' to read archived pre-compaction context; 'list' to list available archives."
        ),
    )
    path: str | None = Field(
        default=None,
        description="Archive file path to read. Omit to read the most recent archive.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Character offset to start reading from (for paging large archives).",
    )
    max_chars: int = Field(
        default=DEFAULT_MAX_CHARS,
        ge=1,
        description="Maximum characters to return in one call.",
    )


class ContextRetrieve(CallableTool2[Params]):
    name: str = "ContextRetrieve"
    params: type[Params] = Params

    def __init__(self, runtime: Runtime) -> None:
        super().__init__(
            description=(
                "Read the exact original text of context that was compacted away.\n\n"
                "Context compaction keeps a summary plus the recent tail; the full "
                "pre-compaction history is archived under the session's archive/ directory. "
                "Use this tool when the summary lacks a detail you need (a specific number, "
                "decision, code snippet, or tool output). 'list' shows available archives; "
                "'read' returns the original messages (page large archives with offset/max_chars)."
            )
        )
        self._archive_dir = Path(runtime.session.dir) / ARCHIVE_DIRNAME

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        try:
            if params.action == "list":
                return self._list()
            return await self._read(params)
        except Exception as e:  # never silent — see module docstring
            logger.warning("ContextRetrieve failed: {error}", error=e)
            return ToolError(message=f"context_retrieve failed: {e}", brief="Retrieve failed")

    def _archives(self) -> list[Path]:
        if not self._archive_dir.is_dir():
            return []
        return sorted(
            (p for p in self._archive_dir.glob("compaction-*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )

    def _list(self) -> ToolReturnValue:
        files = self._archives()
        if not files:
            return ToolError(
                message="No compaction archives found (no compaction has been archived yet).",
                brief="No archives",
            )
        lines = [f"{p}  ({p.stat().st_size} bytes)" for p in files]
        return ToolOk(
            output="\n".join(lines),
            message=f"{len(files)} archive(s) under {self._archive_dir}.",
        )

    async def _read(self, params: Params) -> ToolReturnValue:
        if params.path is None:
            files = self._archives()
            if not files:
                return ToolError(message="No compaction archives found.", brief="No archives")
            path = files[-1]  # latest by mtime
        else:
            path = Path(params.path)
            # Only allow reading files inside the session archive directory.
            try:
                path.resolve().relative_to(self._archive_dir.resolve())
            except ValueError:
                return ToolError(
                    message="Path must be a file inside the session archive directory.",
                    brief="Invalid path",
                )
            if not path.is_file():
                return ToolError(message=f"Archive not found: {params.path}", brief="Not found")

        rendered = await asyncio.to_thread(self._render, path)
        total = len(rendered)
        start = params.offset
        if total > 0 and start >= total:
            return ToolError(
                message=f"offset {start} is past the end (archive has {total} chars).",
                brief="Offset out of range",
            )
        chunk = rendered[start : start + params.max_chars]
        end = start + len(chunk)
        message = f"Archive {path.name}: showing chars {start}-{end} of {total}."
        if end < total:
            message += f" More available — call again with offset={end}."
        else:
            message += " End of archive reached."
        return ToolOk(output=chunk, message=message)

    @staticmethod
    def _render(path: Path) -> str:
        out: list[str] = []
        with path.open(encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    msg = Message.model_validate(json.loads(line, strict=False))
                except (ValidationError, json.JSONDecodeError):
                    continue
                out.append(f"## Message {i}\nRole: {msg.role}\nContent:\n")
                for part in msg.content:
                    text = getattr(part, "text", None)
                    if text:
                        out.append(text)
                for tc in msg.tool_calls or ():
                    out.append(f"[tool_call] {tc.function.name}({tc.function.arguments})")
                out.append("")
        return "\n".join(out)
