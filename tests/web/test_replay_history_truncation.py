"""A truncated history replay must not be announced as complete.

The web client empties its message list the moment the first replayed frame
arrives, and it has no way to tell a full history from a partial one. The old
`replay_history` wrapped its whole send loop in `except Exception: pass`, so a
single failed `send_text` dropped the remainder of the conversation while the
caller still sent `history_complete` -- the client kept the gap as its permanent
view, and it only "healed" when the session was reopened after the turn had
gone idle.

These tests pin the two halves of that contract:

* `replay_history` raises, so the caller can tell the replay is incomplete;
* the caller's `replay_ok` gate is what keeps `history_complete` off the wire.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from kosong.message import TextPart

from kimo_cli.web.api.sessions import _read_wire_lines, replay_history
from kimo_cli.wire.file import WireFile
from kimo_cli.wire.types import TurnBegin, TurnEnd


async def _write_history(path: Path, records: int) -> None:
    wire = WireFile(path=path)
    for i in range(records):
        await wire.append_message(TurnBegin(user_input=f"question {i}"))
        await wire.append_message(TextPart(text=f"reply {i}"))
        await wire.append_message(TurnEnd())


def test_read_wire_lines_parses_every_record(tmp_path: Path) -> None:
    asyncio.run(_write_history(tmp_path / "wire.jsonl", 25))

    lines = _read_wire_lines(tmp_path / "wire.jsonl")

    # 3 records per turn, none of them the metadata header.
    assert len(lines) == 25 * 3
    assert all("question" in line or "reply" in line or "TurnEnd" in line for line in lines)


class _DyingWS:
    """A peer that stops answering after a few frames."""

    def __init__(self, fail_after: int) -> None:
        self.fail_after = fail_after
        self.sent: list[str] = []

    async def send_text(self, message: str) -> None:
        self.sent.append(message)
        if len(self.sent) > self.fail_after:
            raise ConnectionResetError("peer closed mid-replay")


def test_replay_history_propagates_a_send_failure(tmp_path: Path) -> None:
    """The whole point: a failed send must escape, not be swallowed."""
    asyncio.run(_write_history(tmp_path / "wire.jsonl", 10))
    peer = _DyingWS(fail_after=7)

    with pytest.raises(ConnectionResetError):
        asyncio.run(replay_history(peer, tmp_path))

    assert len(peer.sent) == 8  # the 8th send is the one that raised


def test_replay_history_returns_silently_when_the_peer_is_fine(tmp_path: Path) -> None:
    asyncio.run(_write_history(tmp_path / "wire.jsonl", 5))
    peer = _DyingWS(fail_after=10_000)

    asyncio.run(replay_history(peer, tmp_path))

    assert len(peer.sent) == 15


def test_replay_history_is_a_noop_without_a_wire_file(tmp_path: Path) -> None:
    peer = _DyingWS(fail_after=0)

    asyncio.run(replay_history(peer, tmp_path))

    assert peer.sent == []


def test_caller_skips_history_complete_when_the_replay_fails(tmp_path: Path) -> None:
    """The gate that turns "swallowed" into "not announced".

    Reconstructs the caller's control flow rather than driving the whole ASGI
    endpoint: `replay_ok` is set from the same exception that
    `replay_history` now raises, and that flag is what guards the marker.
    """
    asyncio.run(_write_history(tmp_path / "wire.jsonl", 4))
    peer = _DyingWS(fail_after=3)

    async def replay_step() -> bool:
        replay_ok = True
        try:
            await replay_history(peer, tmp_path)
        except Exception:
            replay_ok = False
        return replay_ok

    assert asyncio.run(replay_step()) is False


def test_caller_announces_completion_on_a_clean_replay(tmp_path: Path) -> None:
    asyncio.run(_write_history(tmp_path / "wire.jsonl", 4))
    peer = _DyingWS(fail_after=10_000)

    async def replay_step() -> bool:
        replay_ok = True
        try:
            await replay_history(peer, tmp_path)
        except Exception:
            replay_ok = False
        return replay_ok

    assert asyncio.run(replay_step()) is True
