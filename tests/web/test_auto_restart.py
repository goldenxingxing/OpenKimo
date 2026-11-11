"""Tests for auto-restart of a session worker after an unexpected exit.

The web session worker can die mid-turn (crash, stall, OOM). Without recovery
the session stays in the sticky ``error`` state until the user manually sends a
message; ``_schedule_auto_restart`` restarts it instead, with guards against
crash loops.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import kimo_cli.web.runner.process as process_module
from kimo_cli.web.runner.process import SessionProcess


@pytest.fixture(autouse=True)
def fast_auto_restart(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(process_module, "AUTO_RESTART_DELAY_S", 0.01)


def _make_alive_process() -> MagicMock:
    mock_process = MagicMock()
    mock_process.returncode = None
    return mock_process


@pytest.mark.asyncio
async def test_auto_restart_restarts_dead_worker() -> None:
    sp = SessionProcess(uuid4())
    assert not sp.is_alive

    restarted = asyncio.Event()
    reasons: list[str | None] = []

    async def fake_restart_worker(*, reason: str | None = None) -> None:
        reasons.append(reason)
        restarted.set()

    sp.restart_worker = fake_restart_worker  # type: ignore[method-assign]

    sp._schedule_auto_restart()
    await asyncio.wait_for(restarted.wait(), timeout=5)

    assert reasons == ["auto_recover"]
    assert len(sp._auto_restart_history) == 1


@pytest.mark.asyncio
async def test_auto_restart_bails_when_worker_is_alive() -> None:
    sp = SessionProcess(uuid4())
    sp._process = _make_alive_process()

    restarted = asyncio.Event()

    async def fake_restart_worker(*, reason: str | None = None) -> None:
        restarted.set()

    sp.restart_worker = fake_restart_worker  # type: ignore[method-assign]

    sp._schedule_auto_restart()
    await asyncio.sleep(0.5)

    assert not restarted.is_set()
    assert sp._auto_restart_history == []


@pytest.mark.asyncio
async def test_auto_restart_bails_when_stop_is_expected() -> None:
    sp = SessionProcess(uuid4())
    sp._expecting_exit = True

    restarted = asyncio.Event()

    async def fake_restart_worker(*, reason: str | None = None) -> None:
        restarted.set()

    sp.restart_worker = fake_restart_worker  # type: ignore[method-assign]

    sp._schedule_auto_restart()
    await asyncio.sleep(0.5)

    assert not restarted.is_set()


@pytest.mark.asyncio
async def test_auto_restart_skips_short_lived_worker() -> None:
    """A worker that exits within seconds of starting is persistently crashing;
    restarting it would just crash-loop."""
    sp = SessionProcess(uuid4())
    sp._worker_started_at = time.monotonic()  # died ~now

    restarted = asyncio.Event()

    async def fake_restart_worker(*, reason: str | None = None) -> None:
        restarted.set()

    sp.restart_worker = fake_restart_worker  # type: ignore[method-assign]

    sp._schedule_auto_restart()
    await asyncio.sleep(0.5)

    assert not restarted.is_set()
    assert sp._auto_restart_history == []


@pytest.mark.asyncio
async def test_auto_restart_gives_up_after_max_attempts() -> None:
    sp = SessionProcess(uuid4())
    now = time.monotonic()
    sp._auto_restart_history = [now - 10, now - 20, now - 30]

    restarted = asyncio.Event()

    async def fake_restart_worker(*, reason: str | None = None) -> None:
        restarted.set()

    sp.restart_worker = fake_restart_worker  # type: ignore[method-assign]

    sp._schedule_auto_restart()
    await asyncio.sleep(0.5)

    assert not restarted.is_set()


@pytest.mark.asyncio
async def test_auto_restart_history_expires_after_window() -> None:
    """Old attempts outside the window no longer count against the cap."""
    sp = SessionProcess(uuid4())
    now = time.monotonic()
    sp._auto_restart_history = [
        now - process_module.AUTO_RESTART_WINDOW_S - 1,
        now - process_module.AUTO_RESTART_WINDOW_S - 2,
    ]

    restarted = asyncio.Event()

    async def fake_restart_worker(*, reason: str | None = None) -> None:
        restarted.set()

    sp.restart_worker = fake_restart_worker  # type: ignore[method-assign]

    sp._schedule_auto_restart()
    await asyncio.wait_for(restarted.wait(), timeout=5)

    # The two expired attempts were purged; only the fresh one remains.
    assert len(sp._auto_restart_history) == 1
