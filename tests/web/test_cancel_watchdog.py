"""Tests for the cancel watchdog.

The stop button forwards a ``cancel`` and the UI then waits for the session to
go non-busy. When the worker's unwind wedges (observed on Windows), that wait
was unbounded. The watchdog bounds it: still busy ``CANCEL_WATCHDOG_S`` after
the cancel, and the worker is terminated — the session lives on disk, and the
next prompt auto-starts a fresh worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import kimo_cli.web.runner.process as process_module
from kimo_cli.web.runner.process import SessionProcess


@pytest.fixture(autouse=True)
def fast_watchdog(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(process_module, "CANCEL_WATCHDOG_S", 0.05)


@pytest.mark.asyncio
async def test_watchdog_terminates_a_worker_that_ignores_the_cancel() -> None:
    sp = SessionProcess(uuid4())
    sp._in_flight_prompt_ids.add("prompt-1")
    stopped = asyncio.Event()

    async def fake_stop_worker(*, reason: str | None = None, emit_status: bool = True) -> None:
        assert reason == "cancel_watchdog"
        stopped.set()

    sp.stop_worker = fake_stop_worker  # type: ignore[method-assign]

    await asyncio.wait_for(sp._cancel_watchdog({"prompt-1"}), timeout=5)

    assert stopped.is_set()


@pytest.mark.asyncio
async def test_watchdog_stands_down_when_the_cancelled_turn_ends() -> None:
    sp = SessionProcess(uuid4())
    sp._in_flight_prompt_ids.add("prompt-1")

    stop_worker = AsyncMock()
    sp.stop_worker = stop_worker  # type: ignore[method-assign]

    async def answer_prompt_later() -> None:
        await asyncio.sleep(0.02)
        sp._in_flight_prompt_ids.clear()
        # A prompt sent after the cancel starts its own turn: the watchdog
        # only tracks the cancelled one.
        sp._in_flight_prompt_ids.add("prompt-2")

    await asyncio.gather(sp._cancel_watchdog({"prompt-1"}), answer_prompt_later())
    await asyncio.sleep(0.2)

    stop_worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_watchdog_ignores_prompts_that_started_after_the_cancel() -> None:
    """The intersection check: only prompts in flight at cancel time count."""
    sp = SessionProcess(uuid4())
    sp._in_flight_prompt_ids.add("prompt-2")  # a later turn, slow on its own

    stop_worker = AsyncMock()
    sp.stop_worker = stop_worker  # type: ignore[method-assign]

    await asyncio.wait_for(sp._cancel_watchdog({"prompt-1"}), timeout=5)
    await asyncio.sleep(0.1)

    stop_worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_worker_cancels_an_armed_watchdog() -> None:
    """A deliberate stop supersedes the watchdog — no double termination."""
    sp = SessionProcess(uuid4())
    sp._in_flight_prompt_ids.add("prompt-1")
    sp._arm_cancel_watchdog({"prompt-1"})
    task = sp._cancel_watchdog_task
    assert task is not None and not task.done()

    # No process and no read task: stop_worker takes the fast path through,
    # only cancelling the watchdog and clearing state.
    sp._process = None
    sp._read_task = None

    await asyncio.wait_for(sp.stop_worker(reason="stop"), timeout=5)
    await asyncio.sleep(0.01)

    assert sp._cancel_watchdog_task is None
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_send_message_arms_the_watchdog_for_a_busy_cancel() -> None:
    sp = SessionProcess(uuid4())
    sp._in_flight_prompt_ids.add("prompt-1")

    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    process = MagicMock()
    process.returncode = None  # alive
    process.stdin = stdin
    sp._process = process

    async def fake_start(**kwargs) -> None:
        return None

    sp.start = fake_start  # type: ignore[method-assign]

    message = json.dumps({"jsonrpc": "2.0", "method": "cancel", "id": "cancel-1"})
    await asyncio.wait_for(sp.send_message(message), timeout=5)

    assert sp._cancel_watchdog_task is not None
    sp._cancel_watchdog_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sp._cancel_watchdog_task
