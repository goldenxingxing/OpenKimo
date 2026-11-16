"""A finished background task should speak up on its own over ACP too.

The ACP client only runs a turn when it sends one, so without a follow-up
loop the result of a background task sat unread until the user's next prompt.
Same product behavior the wire server and the interactive shell already have.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from kosong.tooling.empty import EmptyToolset

from kimo_cli.acp.session import ACPSession
from kimo_cli.app import KimoCLI
from kimo_cli.soul.agent import Agent, Runtime
from kimo_cli.soul.context import Context
from kimo_cli.soul.kimosoul import KimoSoul
from kimo_cli.wire.types import TextPart


class _FakeConn:
    async def session_update(self, session_id: str, update: object) -> None:
        pass


def _make_session(runtime: Runtime, tmp_path: Path) -> ACPSession:
    runtime.config.background.auto_followup_coalesce_ms = 0
    runtime.config.background.wait_poll_interval_ms = 50
    soul = KimoSoul(
        Agent(
            name="Follow-up Test Agent",
            system_prompt="Test prompt.",
            toolset=EmptyToolset(),
            runtime=runtime,
        ),
        context=Context(file_backend=tmp_path / "history.jsonl"),
    )
    return ACPSession("session-1", KimoCLI(soul, runtime, {}), _FakeConn())  # type: ignore[arg-type]


async def _drive(session: ACPSession, runtime: Runtime, *, pending: bool) -> list[str]:
    """Let the idle loop run briefly and report the turns it started."""
    started: list[str] = []
    state = {"pending": pending}

    async def fake_turn() -> None:
        started.append("turn")
        state["pending"] = False  # the turn consumes the notification

    session._run_followup_turn = fake_turn  # type: ignore[method-assign]
    runtime.notifications.has_pending_for_sink = lambda sink: state["pending"]  # type: ignore[method-assign]

    assert session._followup_task is not None
    await asyncio.sleep(0.3)
    session._followup_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await session._followup_task
    return started


@pytest.mark.asyncio
async def test_a_completion_while_idle_starts_a_turn(runtime: Runtime, tmp_path: Path) -> None:
    session = _make_session(runtime, tmp_path)
    session._followup_armed = True

    assert await _drive(session, runtime, pending=True) == ["turn"]


@pytest.mark.asyncio
async def test_nothing_happens_before_the_user_has_spoken(runtime: Runtime, tmp_path: Path) -> None:
    """Loading a session with old notifications pending must not start it talking."""
    session = _make_session(runtime, tmp_path)
    session._followup_armed = False

    assert await _drive(session, runtime, pending=True) == []


@pytest.mark.asyncio
async def test_nothing_happens_when_there_is_nothing_to_report(
    runtime: Runtime, tmp_path: Path
) -> None:
    session = _make_session(runtime, tmp_path)
    session._followup_armed = True

    assert await _drive(session, runtime, pending=False) == []


@pytest.mark.asyncio
async def test_a_running_prompt_is_left_to_read_it_itself(runtime: Runtime, tmp_path: Path) -> None:
    """A prompt turn in flight already picks the notification up; two would collide."""
    session = _make_session(runtime, tmp_path)
    session._followup_armed = True
    session._turn_state = object()  # type: ignore[assignment]  # i.e. a prompt is running

    assert await _drive(session, runtime, pending=True) == []


@pytest.mark.asyncio
async def test_it_gives_up_after_repeated_failures(runtime: Runtime, tmp_path: Path) -> None:
    """A broken provider must not be retried on every completion forever."""
    session = _make_session(runtime, tmp_path)
    session._followup_armed = True
    session._followup_failures = session._MAX_FOLLOWUP_FAILURES

    assert await _drive(session, runtime, pending=True) == []


@pytest.mark.asyncio
async def test_a_failing_turn_is_counted_and_does_not_kill_the_loop(
    runtime: Runtime, tmp_path: Path
) -> None:
    session = _make_session(runtime, tmp_path)
    session._followup_armed = True
    runtime.notifications.has_pending_for_sink = lambda sink: True  # type: ignore[method-assign]

    async def boom() -> None:
        raise RuntimeError("provider is down")

    session._run_followup_turn = boom  # type: ignore[method-assign]

    assert session._followup_task is not None
    await asyncio.sleep(0.3)
    assert session._followup_failures >= 1
    assert not session._followup_task.done(), "one bad turn must not end the loop"
    session._followup_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await session._followup_task


@pytest.mark.asyncio
async def test_followup_turn_runs_the_wake_up_prompt(runtime: Runtime, tmp_path: Path) -> None:
    """The wake-up turn goes through cli.run with the shared follow-up prompt."""

    class _FakeCLI:
        def __init__(self, soul: KimoSoul) -> None:
            self._soul = soul
            self.inputs: list[str] = []

        @property
        def soul(self) -> KimoSoul:
            return self._soul

        async def run(self, user_input, cancel_event):
            if isinstance(user_input, str):
                self.inputs.append(user_input)
            yield TextPart(text="reporting")

    runtime.config.background.auto_followup_coalesce_ms = 0
    runtime.config.background.wait_poll_interval_ms = 50
    soul = KimoSoul(
        Agent(
            name="Follow-up Test Agent",
            system_prompt="Test prompt.",
            toolset=EmptyToolset(),
            runtime=runtime,
        ),
        context=Context(file_backend=tmp_path / "history.jsonl"),
    )
    fake_cli = _FakeCLI(soul)
    session = ACPSession("session-1", fake_cli, _FakeConn())  # type: ignore[arg-type]
    session._followup_armed = True
    runtime.notifications.has_pending_for_sink = lambda sink: True  # type: ignore[method-assign]

    assert session._followup_task is not None
    await asyncio.sleep(0.3)
    session._followup_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await session._followup_task

    assert fake_cli.inputs and all(i == ACPSession._FOLLOWUP_PROMPT for i in fake_cli.inputs)
