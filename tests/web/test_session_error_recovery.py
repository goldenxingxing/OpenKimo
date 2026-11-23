"""Tests for session error recovery in process.py and sessions.py.

Verifies that _in_flight_prompt_ids is properly cleared on errors so
that sessions don't get stuck in a permanent "busy" state.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from kimo_cli.web.models import SessionStatus
from kimo_cli.web.runner.process import SessionProcess

# ---------------------------------------------------------------------------
# Tests: SessionProcess.clear_in_flight
# ---------------------------------------------------------------------------


def test_clear_in_flight_resets_is_busy() -> None:
    """clear_in_flight should empty _in_flight_prompt_ids so is_busy is False."""
    sp = SessionProcess(uuid4())
    sp._in_flight_prompt_ids.add("prompt-1")
    sp._in_flight_prompt_ids.add("prompt-2")

    assert sp.is_busy is True

    sp.clear_in_flight()

    assert sp.is_busy is False
    assert len(sp._in_flight_prompt_ids) == 0


# ---------------------------------------------------------------------------
# Tests: _read_loop clears in-flight on unexpected exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_loop_clears_in_flight_on_exception() -> None:
    """When _read_loop encounters a non-EOF, non-CancelledError exception,
    it should clear _in_flight_prompt_ids and emit an error status.
    """
    sp = SessionProcess(uuid4())
    sp._in_flight_prompt_ids.add("prompt-in-flight")
    assert sp.is_busy is True

    # Create a mock process whose stdout has one line then EOF
    mock_stdout = asyncio.StreamReader()
    mock_stdout.feed_data(b"not-valid-json\n")
    mock_stdout.feed_eof()

    mock_stderr = asyncio.StreamReader()
    mock_stderr.feed_data(b"mock stderr")
    mock_stderr.feed_eof()

    mock_process = MagicMock()
    mock_process.stdout = mock_stdout
    mock_process.stderr = mock_stderr
    mock_process.returncode = None

    sp._process = mock_process

    auto_restart_calls: list[None] = []
    sp._schedule_auto_restart = lambda: auto_restart_calls.append(None)  # type: ignore[assignment]

    # _broadcast raises on the first call (the stdout line),
    # succeeds on subsequent calls (_emit_status broadcast).
    call_count = 0

    async def failing_then_ok_broadcast(msg: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("broadcast failed")

    sp._broadcast = failing_then_ok_broadcast  # type: ignore[assignment]

    await sp._read_loop()

    assert sp.is_busy is False
    assert sp.status.state == "error"
    assert sp.status.reason == "read_loop_error"
    assert len(auto_restart_calls) == 1


# ---------------------------------------------------------------------------
# Tests: EOF path clears in-flight before broadcast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_loop_eof_clears_in_flight_before_broadcast() -> None:
    """On worker process EOF, _in_flight_prompt_ids should be cleared
    before _broadcast is called, so that is_busy is already False when
    the frontend reacts to the error.
    """
    sp = SessionProcess(uuid4())
    sp._in_flight_prompt_ids.add("prompt-in-flight")

    # Track is_busy at the time broadcast is called
    busy_at_broadcast: list[bool] = []

    async def tracking_broadcast(msg: str) -> None:
        busy_at_broadcast.append(sp.is_busy)

    sp._broadcast = tracking_broadcast  # type: ignore[assignment]

    # Create a mock process that immediately returns EOF
    mock_stdout = asyncio.StreamReader()
    mock_stdout.feed_eof()

    mock_stderr = asyncio.StreamReader()
    mock_stderr.feed_data(b"worker crashed")
    mock_stderr.feed_eof()

    mock_process = MagicMock()
    mock_process.stdout = mock_stdout
    mock_process.stderr = mock_stderr
    mock_process.returncode = 1

    sp._process = mock_process
    sp._expecting_exit = False

    auto_restart_calls: list[None] = []
    sp._schedule_auto_restart = lambda: auto_restart_calls.append(None)  # type: ignore[assignment]

    await sp._read_loop()

    # At the time broadcast was called, is_busy should already be False
    assert busy_at_broadcast[0] is False
    assert sp.status.state == "error"
    assert len(auto_restart_calls) == 1


# ---------------------------------------------------------------------------
# Tests: a silent worker exit names the log file holding the traceback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_loop_silent_exit_names_log_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that dies after `enable_logging` leaves an empty stderr pipe:
    its fd=2 is redirected into loguru. The substituted message must say which
    file the traceback landed in, not just "check the logs".
    """
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))

    sp = SessionProcess(uuid4())

    broadcast: list[str] = []

    async def capture(msg: str) -> None:
        broadcast.append(msg)

    sp._broadcast = capture  # type: ignore[assignment]

    mock_stdout = asyncio.StreamReader()
    mock_stdout.feed_eof()

    mock_stderr = asyncio.StreamReader()
    mock_stderr.feed_eof()  # nothing on the pipe: the traceback went to the log

    mock_process = MagicMock()
    mock_process.stdout = mock_stdout
    mock_process.stderr = mock_stderr
    mock_process.returncode = 1

    sp._process = mock_process
    sp._expecting_exit = False

    auto_restart_calls: list[None] = []
    sp._schedule_auto_restart = lambda: auto_restart_calls.append(None)  # type: ignore[assignment]

    await sp._read_loop()

    expected = str(tmp_path / "logs" / "kimo.log")
    # broadcast is a JSON-encoded wire message: a Windows backslash path is
    # escaped (\\), so compare against the escaped form.
    assert json.dumps(expected)[1:-1] in broadcast[0]
    assert "exit code 1" in broadcast[0]
    assert sp.status.detail is not None
    assert expected in sp.status.detail


# ---------------------------------------------------------------------------
# Tests: one malformed worker line must not kill the read loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_loop_skips_malformed_lines_and_keeps_reading() -> None:
    """A bare wire message (the unwrapped-StatusUpdate regression) fails
    JSONRPCSuccessResponse validation. That must be logged and skipped —
    killing the read loop over one bad line ended the turn for every client
    and marked the session "error"."""
    import json as jsonlib

    sp = SessionProcess(uuid4())

    malformed = {"context_usage": None, "waiting_on_background": True}
    valid = {
        "jsonrpc": "2.0",
        "id": "req-1",
        "error": {"code": -32000, "message": "session busy"},
    }

    mock_stdout = asyncio.StreamReader()
    for payload in (malformed, valid):
        mock_stdout.feed_data(jsonlib.dumps(payload).encode() + b"\n")

    mock_process = MagicMock()
    mock_process.stdout = mock_stdout
    mock_process.stderr = asyncio.StreamReader()
    mock_process.returncode = None

    sp._process = mock_process

    broadcast: list[str] = []

    async def capture(msg: str) -> None:
        broadcast.append(msg)

    sp._broadcast = capture  # type: ignore[assignment]

    auto_restart_calls: list[None] = []
    sp._schedule_auto_restart = lambda: auto_restart_calls.append(None)  # type: ignore[assignment]

    handled: list[str] = []

    async def record_handle(message: object) -> None:
        handled.append(str(message))

    sp._handle_out_message = record_handle  # type: ignore[assignment]

    task = asyncio.create_task(sp._read_loop())
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(broadcast) == 2, "both lines must reach the broadcast"
    assert len(handled) == 1, "only the valid message may be dispatched"
    assert sp.status.state == "stopped", "a skipped line must not mark the session error"
    assert auto_restart_calls == []


# ---------------------------------------------------------------------------
# Tests: error state allows recovery with new prompt
# ---------------------------------------------------------------------------


def test_session_in_error_state_clears_stale_ids_on_new_prompt() -> None:
    """When a session is in error state and is_busy is True (stale IDs),
    clear_in_flight should be callable to recover the session.
    This tests the building block used by the sessions.py WebSocket handler.
    """
    session_id = uuid4()
    sp = SessionProcess(session_id)

    # Simulate: session errored out but _in_flight_prompt_ids was not cleared
    sp._in_flight_prompt_ids.add("stale-prompt")
    sp._status = SessionStatus(
        session_id=session_id,
        state="error",
        seq=1,
        worker_id=None,
        reason="process_exit",
        detail=None,
        updated_at=datetime.now(UTC),
    )

    assert sp.is_busy is True
    assert sp.status.state == "error"

    # The sessions.py handler checks this condition and calls clear_in_flight
    if sp.status.state == "error" and sp.is_busy:
        sp.clear_in_flight()

    assert sp.is_busy is False
