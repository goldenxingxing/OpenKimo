"""Regression tests for prompt send and worker-restart lifecycle races."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import kimo_cli.web.runner.process as process_module
from kimo_cli.web.models import SessionState
from kimo_cli.web.runner.process import KimoCLIRunner, SessionProcess


def _prompt(prompt_id: str) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "prompt",
            "id": prompt_id,
            "params": {"user_input": "hello"},
        }
    )


def _process_with_stdin() -> MagicMock:
    process = MagicMock()
    process.returncode = None
    process.stdin = MagicMock()
    process.stdin.write = MagicMock()
    process.stdin.drain = AsyncMock()
    return process


def _ready_for_send(sp: SessionProcess) -> MagicMock:
    process = _process_with_stdin()
    sp._process = process
    sp.start = AsyncMock()  # type: ignore[method-assign]
    sp._encode_uploaded_files = AsyncMock(return_value=([], {}))  # type: ignore[method-assign]
    return process


@pytest.mark.asyncio
@pytest.mark.parametrize("other_prompt_in_flight", [False, True])
async def test_attachment_preprocessing_failure_rolls_back_only_its_prompt(
    other_prompt_in_flight: bool,
) -> None:
    sp = SessionProcess(uuid4())
    _ready_for_send(sp)
    if other_prompt_in_flight:
        sp._in_flight_prompt_ids.add("other-prompt")
        sp._build_status("busy", "prompt", None)
    sp._encode_uploaded_files = AsyncMock(side_effect=OSError("encode failed"))  # type: ignore[method-assign]

    with pytest.raises(OSError, match="encode failed"):
        await sp.send_message(_prompt("failed-prompt"))

    assert "failed-prompt" not in sp._in_flight_prompt_ids
    assert sp.is_busy is other_prompt_in_flight
    assert sp.status.state == ("busy" if other_prompt_in_flight else "idle")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["write", "drain"])
async def test_stdin_failure_rolls_back_prompt_and_busy(failure_stage: str) -> None:
    sp = SessionProcess(uuid4())
    process = _ready_for_send(sp)
    if failure_stage == "write":
        process.stdin.write.side_effect = BrokenPipeError("write failed")
    else:
        process.stdin.drain.side_effect = BrokenPipeError("drain failed")

    with pytest.raises(BrokenPipeError):
        await sp.send_message(_prompt("failed-prompt"))

    assert not sp.is_busy
    assert sp._in_flight_prompt_ids == set()
    assert sp.status.state == "idle"
    assert sp.status.reason == "prompt_send_failed"


@pytest.mark.asyncio
async def test_delayed_auto_restart_cannot_survive_deliberate_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_module, "AUTO_RESTART_DELAY_S", 0.02)
    sp = SessionProcess(uuid4())
    sp.restart_worker = AsyncMock()  # type: ignore[method-assign]

    sp._schedule_auto_restart()
    await sp.stop_worker(reason="stop")
    await asyncio.sleep(0.06)

    sp.restart_worker.assert_not_awaited()
    assert not sp.is_alive
    assert sp.status.state == "stopped"


@pytest.mark.asyncio
async def test_forget_invalidates_delayed_auto_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_module, "AUTO_RESTART_DELAY_S", 0.02)
    runner = KimoCLIRunner()
    session_id = uuid4()
    sp = SessionProcess(session_id)
    sp.restart_worker = AsyncMock()  # type: ignore[method-assign]
    runner._sessions[session_id] = sp

    sp._schedule_auto_restart()
    await runner.forget_session(session_id)
    await asyncio.sleep(0.06)

    sp.restart_worker.assert_not_awaited()
    assert runner.get_session(session_id) is None


@pytest.mark.asyncio
async def test_prompt_entering_restart_status_gap_defers_termination() -> None:
    sp = SessionProcess(uuid4())
    process = _ready_for_send(sp)
    restarting = asyncio.Event()
    release_restart = asyncio.Event()
    original_emit_status = sp._emit_status

    async def block_restarting_status(
        state: SessionState,
        *,
        reason: str | None = None,
        detail: str | None = None,
    ) -> None:
        await original_emit_status(state, reason=reason, detail=detail)
        if state == "restarting":
            restarting.set()
            await release_restart.wait()

    sp._emit_status = block_restarting_status  # type: ignore[method-assign]

    restart_task = asyncio.create_task(sp.restart_worker(reason="config_update"))
    await asyncio.wait_for(restarting.wait(), timeout=1)
    # This is the former race window: idle was observed before the status
    # await, but this prompt becomes busy before termination actually starts.
    await sp.send_message(_prompt("new-prompt"))
    assert sp.is_busy

    release_restart.set()
    await asyncio.wait_for(restart_task, timeout=1)

    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    process.stdin.write.assert_called_once()
    assert sp._in_flight_prompt_ids == {"new-prompt"}
    assert sp._deferred_restart_reason == "config_update"
    assert sp.status.state == "busy"


@pytest.mark.asyncio
async def test_stop_during_deferred_restart_status_gap_prevents_replacement() -> None:
    sp = SessionProcess(uuid4())
    process = _ready_for_send(sp)
    process.wait = AsyncMock(return_value=0)
    start_mock = AsyncMock()
    sp.start = start_mock  # type: ignore[method-assign]
    restarting = asyncio.Event()
    release_restart = asyncio.Event()
    original_emit_status = sp._emit_status

    async def block_restarting_status(
        state: SessionState,
        *,
        reason: str | None = None,
        detail: str | None = None,
    ) -> None:
        await original_emit_status(state, reason=reason, detail=detail)
        if state == "restarting":
            restarting.set()
            await release_restart.wait()

    sp._emit_status = block_restarting_status  # type: ignore[method-assign]
    sp.defer_restart("config_update")

    restart_task = asyncio.create_task(sp._apply_deferred_restart())
    await asyncio.wait_for(restarting.wait(), timeout=1)
    await sp.stop_worker(reason="stop")
    release_restart.set()
    await asyncio.wait_for(restart_task, timeout=1)

    start_mock.assert_not_awaited()
    assert not sp.is_alive
    assert sp.status.state == "stopped"
