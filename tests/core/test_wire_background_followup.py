"""A finished background task should speak up on its own.

The interactive shell has always done this. A client that only runs a turn
when it is sent one had no such loop, so the result sat unread: the foreground
session had ended, and the answer waited for the user to type something
unrelated before it surfaced.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from kosong.tooling.empty import EmptyToolset

from kimo_cli.soul.agent import Agent, Runtime
from kimo_cli.soul.context import Context
from kimo_cli.soul.kimosoul import KimoSoul
from kimo_cli.wire.jsonrpc import JSONRPCCancelMessage
from kimo_cli.wire.server import WireServer


def _make_soul(runtime: Runtime, tmp_path: Path) -> KimoSoul:
    agent = Agent(
        name="Follow-up Test Agent",
        system_prompt="Test prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    return KimoSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))


async def _drive(server: WireServer, runtime: Runtime, *, pending: bool) -> list[str]:
    """Run one pass of the loop and report the turns it started."""
    started: list[str] = []

    async def fake_turn() -> None:
        started.append("turn")

    server._run_followup_turn = fake_turn  # type: ignore[method-assign]
    runtime.notifications.has_pending_for_sink = lambda sink: pending  # type: ignore[method-assign]
    runtime.config.background.auto_followup_coalesce_ms = 0

    task = asyncio.create_task(server._background_followup_loop())
    runtime.background_tasks.completion_event.set()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return started


@pytest.mark.asyncio
async def test_a_completion_while_idle_starts_a_turn(runtime: Runtime, tmp_path: Path) -> None:
    server = WireServer(_make_soul(runtime, tmp_path))
    server._followup_armed = True

    assert await _drive(server, runtime, pending=True) == ["turn"]


@pytest.mark.asyncio
async def test_nothing_happens_before_the_user_has_spoken(runtime: Runtime, tmp_path: Path) -> None:
    """Reopening a session with old notifications must not start it talking."""
    server = WireServer(_make_soul(runtime, tmp_path))
    server._followup_armed = False

    assert await _drive(server, runtime, pending=True) == []


@pytest.mark.asyncio
async def test_nothing_happens_when_there_is_nothing_to_report(
    runtime: Runtime, tmp_path: Path
) -> None:
    server = WireServer(_make_soul(runtime, tmp_path))
    server._followup_armed = True

    assert await _drive(server, runtime, pending=False) == []


@pytest.mark.asyncio
async def test_a_running_turn_is_left_to_read_it_itself(runtime: Runtime, tmp_path: Path) -> None:
    """A turn in flight already picks the notification up; two would collide."""
    server = WireServer(_make_soul(runtime, tmp_path))
    server._followup_armed = True
    server._cancel_event = asyncio.Event()  # i.e. streaming

    assert await _drive(server, runtime, pending=True) == []


@pytest.mark.asyncio
async def test_it_gives_up_after_repeated_failures(runtime: Runtime, tmp_path: Path) -> None:
    """A broken provider must not be retried on every completion forever."""
    server = WireServer(_make_soul(runtime, tmp_path))
    server._followup_armed = True
    server._followup_failures = server._MAX_FOLLOWUP_FAILURES

    assert await _drive(server, runtime, pending=True) == []


@pytest.mark.asyncio
async def test_the_loop_drives_reconcile_itself(runtime: Runtime, tmp_path: Path) -> None:
    """While the server sits idle nothing else calls reconcile() — the loop must.

    A task that finishes unnoticed only surfaces when reconcile() scans the
    task store; the loop cannot just wait for completion_event, which nothing
    would ever set. This was the web/desktop gap.
    """
    server = WireServer(_make_soul(runtime, tmp_path))
    server._followup_armed = True
    runtime.config.background.auto_followup_coalesce_ms = 0
    runtime.config.background.wait_poll_interval_ms = 50

    started: list[str] = []
    state = {"pending": False}

    async def fake_turn() -> None:
        started.append("turn")
        state["pending"] = False  # the turn consumes the notification

    server._run_followup_turn = fake_turn  # type: ignore[method-assign]

    reconciled = False

    def fake_reconcile(*args: object, **kwargs: object) -> list[str]:
        nonlocal reconciled
        if not reconciled:
            # the first scan discovers the finished task; publishing is one-shot
            state["pending"] = True
            runtime.notifications.has_pending_for_sink = lambda sink: state["pending"]  # type: ignore[method-assign]
        reconciled = True
        return []

    runtime.background_tasks.reconcile = fake_reconcile  # type: ignore[method-assign]

    task = asyncio.create_task(server._background_followup_loop())
    # poll interval is clamped to >= 0.1s, so allow enough polls for both
    # the True and the False transition to be sampled
    await asyncio.sleep(0.9)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert reconciled, "the loop must poll reconcile() while idle"
    assert started == ["turn"]


@pytest.mark.asyncio
async def test_a_failing_turn_is_counted_and_does_not_kill_the_loop(
    runtime: Runtime, tmp_path: Path
) -> None:
    server = WireServer(_make_soul(runtime, tmp_path))
    server._followup_armed = True
    runtime.notifications.has_pending_for_sink = lambda sink: True  # type: ignore[method-assign]
    runtime.config.background.auto_followup_coalesce_ms = 0

    async def boom() -> None:
        raise RuntimeError("provider is down")

    server._run_followup_turn = boom  # type: ignore[method-assign]

    task = asyncio.create_task(server._background_followup_loop())
    runtime.background_tasks.completion_event.set()
    await asyncio.sleep(0.05)
    assert server._followup_failures >= 1
    assert not task.done(), "one bad turn must not end the loop"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_waiting_transition_is_sent_as_wrapped_event(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The "turn parked on background task(s)" transition must go out wrapped
    in a JSONRPCEventMessage. A bare StatusUpdate on stdout is not parseable
    by the web server's read loop and killed the whole session (the
    read_loop_error → "session error" regression)."""
    from kimo_cli.wire.jsonrpc import JSONRPCEventMessage
    from kimo_cli.wire.serde import deserialize_wire_message
    from kimo_cli.wire.types import StatusUpdate

    server = WireServer(_make_soul(runtime, tmp_path))
    server._followup_armed = True
    runtime.config.background.wait_poll_interval_ms = 20
    runtime.config.background.auto_followup_coalesce_ms = 0
    runtime.notifications.has_pending_for_sink = lambda sink: False  # type: ignore[method-assign]

    sent: list[object] = []

    async def capture(msg: object) -> None:
        sent.append(msg)

    server._send_msg = capture  # type: ignore[method-assign]

    polls = {"n": 0}

    def fake_waiting() -> bool:
        polls["n"] += 1
        return polls["n"] <= 5

    server._waiting_on_background = fake_waiting  # type: ignore[method-assign]

    task = asyncio.create_task(server._background_followup_loop())
    # poll interval is clamped to >= 0.1s, so allow enough polls for both
    # the True and the False transition to be sampled
    await asyncio.sleep(0.9)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert polls["n"] >= 6, f"loop only polled {polls['n']} times"
    assert len(sent) == 2, f"expected both transitions, got: {sent}"
    for msg in sent:
        assert isinstance(msg, JSONRPCEventMessage), f"bare wire message sent: {msg!r}"
        # Mirror the web server's parse path on the exact stdout bytes: the
        # line must be an "event" envelope whose params deserialize to the
        # StatusUpdate — a bare StatusUpdate dict has no "method" and used
        # to kill the read loop here.
        dumped = json.loads(msg.model_dump_json())
        assert dumped["method"] == "event"
        revived = deserialize_wire_message(dumped["params"])
        assert isinstance(revived, StatusUpdate)
        assert revived.waiting_on_background in (True, False)


@pytest.mark.asyncio
async def test_cancel_disarms_auto_followup(runtime: Runtime, tmp_path: Path) -> None:
    """Stop must prevent a later background completion from spinning up a fresh
    autonomous turn. Without this, the user hits Stop, then a still-running
    background task (Playwright, crawler, ...) finishes and
    _background_followup_loop calls _run_followup_turn — the agent talks
    again right after being stopped. The next user prompt re-arms it."""
    server = WireServer(_make_soul(runtime, tmp_path))
    server._followup_armed = True
    server._cancel_event = asyncio.Event()  # i.e. a turn is in progress

    # Mock the background task manager so kill_all_active can be observed
    # without touching real task state on disk.
    kill_calls: list[str] = []

    def _kill_all_active(*, reason: str = "Killed") -> list[str]:
        kill_calls.append(reason)
        return ["bg-task-1", "bg-task-2"]

    runtime.background_tasks.kill_all_active = _kill_all_active  # type: ignore[method-assign]

    msg = JSONRPCCancelMessage(id="cancel-test-1")
    resp = await server._handle_cancel(msg)
    assert resp.result == {}  # stop is accepted, not rejected

    # The cancel event is set (the current turn will unwind).
    assert server._cancel_event.is_set()
    # Auto-follow-up is disarmed so the follow-up loop will not start a new
    # turn when the outstanding background task finishes.
    assert server._followup_armed is False
    # Background tasks are killed too — Stop means "the whole session stops",
    # not just "the current turn ends". Otherwise a Playwright crawler or
    # long-running subagent keeps running and the user perceives it as the
    # agent still going.
    assert kill_calls == ["user_stop"], (
        f"kill_all_active must be called once with reason='user_stop', got {kill_calls}"
    )

    # The flag is sticky only until the user's next prompt: _handle_prompt
    # sets it back to True. Confirm that a background completion while
    # _followup_armed is False is ignored.
    assert await _drive(server, runtime, pending=True) == []
