"""A replacement worker learns what the attached client can do.

`restart_worker` swaps the subprocess while the WebSockets stay attached, and
the browser only sends `initialize` when its socket opens. Without a replay the
new worker keeps the defaults — no question support, no plan mode, no external
tools or hooks — so an AskUserQuestion after a model switch or a config change
came back as "the connected client does not support interactive questions".
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from kimo_cli.web.runner.process import SessionProcess

INITIALIZE = json.dumps(
    {
        "jsonrpc": "2.0",
        "method": "initialize",
        "id": "init-1",
        "params": {
            "protocol_version": "1.10",
            "client": {"name": "kiwi", "version": "0.0.0"},
            "capabilities": {"supports_question": True, "supports_plan_mode": True},
        },
    }
)


def _process_with_stdin() -> tuple[MagicMock, list[bytes]]:
    written: list[bytes] = []
    stdin = MagicMock()
    stdin.write = written.append
    stdin.drain = AsyncMock()
    process = MagicMock()
    process.stdin = stdin
    process.returncode = None
    return process, written


@pytest.mark.asyncio
async def test_initialize_is_remembered_and_replayed() -> None:
    sp = SessionProcess(uuid4())
    process, written = _process_with_stdin()
    sp._process = process
    sp.start = AsyncMock()  # type: ignore[method-assign]

    await sp.send_message(INITIALIZE)
    assert sp._last_initialize == INITIALIZE
    written.clear()

    # What start() does for the worker that replaces this one.
    await sp._replay_initialize()

    assert [json.loads(b.decode())["method"] for b in written] == ["initialize"]
    replayed = json.loads(written[0].decode())
    assert replayed["params"]["capabilities"]["supports_question"] is True


@pytest.mark.asyncio
async def test_replay_is_a_no_op_before_any_client_said_anything() -> None:
    sp = SessionProcess(uuid4())
    process, written = _process_with_stdin()
    sp._process = process

    await sp._replay_initialize()

    assert written == []


@pytest.mark.asyncio
async def test_a_failing_stdin_does_not_break_the_restart() -> None:
    """The restart continues: the worker is up, and only capabilities are lost."""
    sp = SessionProcess(uuid4())
    process = MagicMock()
    process.stdin.write = MagicMock(side_effect=BrokenPipeError("gone"))
    process.stdin.drain = AsyncMock()
    sp._process = process
    sp._last_initialize = INITIALIZE

    await sp._replay_initialize()


@pytest.mark.asyncio
async def test_prompts_are_not_remembered_as_initialize() -> None:
    sp = SessionProcess(uuid4())
    process, _ = _process_with_stdin()
    sp._process = process
    sp.start = AsyncMock()  # type: ignore[method-assign]
    sp._emit_status = AsyncMock()  # type: ignore[method-assign]

    prompt = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "prompt",
            "id": "p-1",
            "params": {"content": [{"type": "text", "text": "hi"}]},
        }
    )
    await sp.send_message(prompt)

    assert sp._last_initialize is None


@pytest.mark.asyncio
async def test_restart_sends_initialize_before_the_idle_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordering matters: a client that sees idle may send a prompt at once."""
    sp = SessionProcess(uuid4())
    sp._last_initialize = INITIALIZE
    events: list[str] = []

    process, written = _process_with_stdin()
    process.stdin.write = lambda data: events.append(json.loads(data.decode())["method"])

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return process

    async def record_status(state: str, **kwargs: object) -> None:
        events.append(f"status:{state}")

    sp._emit_status = record_status  # type: ignore[method-assign]
    sp._read_loop = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await sp.start()

    assert events == ["initialize", "status:idle"]
