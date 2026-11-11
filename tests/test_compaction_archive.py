"""Verification for P1 reversible compaction: archive + ContextRetrieve tool.

The core guarantee is losslessness: archiving a pre-compaction history and
reading it back must reproduce the exact original messages (byte-identical JSON
round-trip), and a missing archive must surface an explicit error rather than a
silent miss.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from kosong.message import Message

from kimo_cli.soul.compaction_archive import CompactionArchiver
from kimo_cli.tools.context_retrieve import ContextRetrieve, Params

# A history exercising text, tool results, tool calls and non-ASCII content.
HISTORY_DICTS = [
    {"role": "user", "content": [{"type": "text", "text": "把阈值改成 55，并验证。"}]},
    {
        "role": "assistant",
        "content": [{"type": "text", "text": "我先读文件。"}],
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "ReadFile", "arguments": json.dumps({"path": "a.py"})},
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": [{"type": "text", "text": "G_safe = 70\nI_max = 9\nτ = 20\n"}],
    },
    {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
]


def _history() -> list[Message]:
    return [Message.model_validate(d) for d in HISTORY_DICTS]


def test_archive_roundtrip_is_lossless(tmp_path: Path) -> None:
    archiver = CompactionArchiver(tmp_path / "archive")
    history = _history()
    path = archiver.archive(history)

    assert path.is_file()
    assert path.parent == tmp_path / "archive"

    # Read the archive back and require byte-identical JSON per message.
    restored = [
        Message.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(restored) == len(history)
    for orig, back in zip(history, restored, strict=True):
        assert orig.model_dump_json(exclude_none=True) == back.model_dump_json(exclude_none=True)


def test_archive_names_are_unique(tmp_path: Path) -> None:
    archiver = CompactionArchiver(tmp_path / "archive")
    a = archiver.archive(_history())
    b = archiver.archive(_history())
    assert a != b
    assert a.parent == b.parent == tmp_path / "archive"


def test_reference_mentions_path_and_tool(tmp_path: Path) -> None:
    archiver = CompactionArchiver(tmp_path / "archive")
    path = archiver.archive(_history())
    ref = CompactionArchiver.reference(path, n_messages=4)
    assert str(path) in ref
    assert "ContextRetrieve" in ref
    assert "4 messages" in ref


def _tool(tmp_path: Path) -> ContextRetrieve:
    runtime = SimpleNamespace(session=SimpleNamespace(dir=tmp_path))
    return ContextRetrieve(runtime)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_tool_list_empty_is_explicit_not_silent(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    result = await tool(Params(action="list"))
    assert "No compaction archives" in result.message


@pytest.mark.asyncio
async def test_tool_read_latest_roundtrip(tmp_path: Path) -> None:
    archiver = CompactionArchiver(tmp_path / "archive")
    history = _history()
    archiver.archive(history)

    tool = _tool(tmp_path)
    result = await tool(Params(action="read"))
    # Rendered text carries the original content (incl. non-ASCII + tool call).
    assert "把阈值改成 55" in result.output
    assert "G_safe = 70" in result.output
    assert "ReadFile" in result.output
    assert "End of archive reached" in result.message


@pytest.mark.asyncio
async def test_tool_read_paging(tmp_path: Path) -> None:
    archiver = CompactionArchiver(tmp_path / "archive")
    archiver.archive(_history())
    tool = _tool(tmp_path)
    full = await tool(Params(action="read"))
    total_chars = len(full.output)

    first = await tool(Params(action="read", offset=0, max_chars=20))
    assert len(first.output) == 20
    assert "More available" in first.message

    rest = await tool(Params(action="read", offset=20, max_chars=total_chars))
    assert first.output + rest.output == full.output


@pytest.mark.asyncio
async def test_tool_read_missing_path_is_explicit(tmp_path: Path) -> None:
    archiver = CompactionArchiver(tmp_path / "archive")
    archiver.archive(_history())
    tool = _tool(tmp_path)
    missing = tmp_path / "archive" / "compaction-20990101-000000-deadbeef.jsonl"
    result = await tool(Params(action="read", path=str(missing)))
    assert "not found" in result.message.lower() or "Archive not found" in result.message


@pytest.mark.asyncio
async def test_tool_rejects_path_outside_archive(tmp_path: Path) -> None:
    archiver = CompactionArchiver(tmp_path / "archive")
    archiver.archive(_history())
    tool = _tool(tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}", encoding="utf-8")
    result = await tool(Params(action="read", path=str(outside)))
    assert "archive directory" in result.message
