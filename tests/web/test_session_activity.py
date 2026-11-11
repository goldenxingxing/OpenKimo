"""A page learns when any of its sessions stops to wait for a person."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import kimo_cli.web.user_auth as user_auth
from kimo_cli.web.api import sessions as sessions_api
from kimo_cli.web.runner.activity import ActivityHub, SessionActivity
from kimo_cli.web.runner.process import KimoCLIRunner, SessionProcess
from kimo_cli.wire.jsonrpc import JSONRPCEventMessage, JSONRPCRequestMessage
from kimo_cli.wire.types import (
    ApprovalRequest,
    ApprovalResponse,
    QuestionItem,
    QuestionOption,
    QuestionRequest,
    StepRetry,
)


def _approval(rpc_id: str, request_id: str) -> JSONRPCRequestMessage:
    return JSONRPCRequestMessage(
        id=rpc_id,
        params=ApprovalRequest(
            id=request_id, tool_call_id="c", sender="Shell", action="run", description="rm"
        ),
    )


def _question(rpc_id: str) -> JSONRPCRequestMessage:
    options = [QuestionOption(label="A"), QuestionOption(label="B")]
    return JSONRPCRequestMessage(
        id=rpc_id,
        params=QuestionRequest(
            id=f"q-{rpc_id}",
            tool_call_id="c",
            questions=[QuestionItem(question="Which?", options=options)],
        ),
    )


@pytest.fixture
def recorded() -> tuple[SessionProcess, list[SessionActivity]]:
    seen: list[SessionActivity] = []
    process = SessionProcess(uuid4(), on_activity=seen.append)

    async def quiet(_message: str) -> None:
        return None

    process._broadcast = quiet  # type: ignore[assignment]
    return process, seen


async def test_a_turn_that_ends_is_published(recorded) -> None:
    process, seen = recorded

    await process._emit_status("busy", reason="prompt")
    await process._emit_status("idle", reason="prompt_complete")

    assert [(a.state, a.awaiting_input) for a in seen] == [("busy", False), ("idle", False)]
    assert [a.seq for a in seen] == [1, 2]


async def test_an_approval_waits_until_it_is_answered(recorded) -> None:
    process, seen = recorded
    await process._emit_status("busy", reason="prompt")

    await process._handle_out_message(_approval("rpc-1", "appr-1"))
    assert seen[-1].state == "busy" and seen[-1].awaiting_input is True

    process._note_answered("rpc-1")
    assert seen[-1].awaiting_input is False
    assert [a.seq for a in seen] == [1, 2, 3]


async def test_an_approval_resolved_elsewhere_stops_waiting(recorded) -> None:
    process, seen = recorded
    await process._emit_status("busy", reason="prompt")
    await process._handle_out_message(_approval("rpc-1", "appr-1"))

    resolved = JSONRPCEventMessage(params=ApprovalResponse(request_id="appr-1", response="approve"))
    await process._handle_out_message(resolved)

    assert seen[-1].awaiting_input is False


async def test_a_question_counts_and_an_ended_turn_clears_it(recorded) -> None:
    process, seen = recorded
    await process._emit_status("busy", reason="prompt")
    await process._handle_out_message(_question("rpc-2"))
    assert seen[-1].awaiting_input is True

    await process._emit_status("idle", reason="prompt_complete")

    assert (seen[-1].state, seen[-1].awaiting_input) == ("idle", False)
    assert process.activity.awaiting_input is False


async def test_a_question_replayed_while_idle_is_not_awaiting(recorded) -> None:
    """History replayed into a fresh client re-emits the old QuestionRequest.

    Its turn died with the worker restart, so the session must not claim to
    be waiting on input nobody can answer or cancel.
    """
    process, seen = recorded
    await process._emit_status("idle", reason="start")

    await process._handle_out_message(_question("rpc-9"))

    assert process._awaiting_requests == {}
    assert process.activity.awaiting_input is False


async def test_nothing_is_published_when_nothing_a_page_shows_changed(recorded) -> None:
    process, seen = recorded
    await process._emit_status("busy", reason="prompt")
    await process._handle_out_message(_approval("rpc-1", "a1"))
    await process._handle_out_message(_approval("rpc-2", "a2"))
    process._note_answered("rpc-1")
    process._note_answered("unknown")

    assert [(a.state, a.awaiting_input) for a in seen] == [("busy", False), ("busy", True)]


def test_a_page_that_stops_reading_keeps_only_the_newest() -> None:
    hub = ActivityHub()
    queue = hub.subscribe()
    session_id = uuid4()
    for seq in range(300):
        hub.publish(
            SessionActivity(
                session_id=session_id, state="busy", seq=seq, updated_at="2026-09-24T00:00:00Z"
            )
        )

    assert queue.qsize() == 256
    assert queue.get_nowait().seq == 300 - 256
    hub.unsubscribe(queue)
    hub.publish(
        SessionActivity(
            session_id=session_id, state="idle", seq=999, updated_at="2026-09-24T00:00:00Z"
        )
    )
    assert queue.qsize() == 255


async def test_the_stream_carries_only_the_callers_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mine, theirs = uuid4(), uuid4()
    owners = {mine: "u1", theirs: "u2"}
    monkeypatch.setattr(
        sessions_api,
        "load_session_by_id",
        lambda session_id: SimpleNamespace(owner_id=owners.get(session_id)),
    )
    monkeypatch.setattr(user_auth, "get_current_user", lambda _request: {"id": "u1"})

    runner = KimoCLIRunner()
    processes = {}
    for session_id in (mine, theirs):
        process = await runner.get_or_create_session(session_id)
        process._broadcast = _noop  # type: ignore[assignment]
        await process._emit_status("busy", reason="prompt")
        processes[session_id] = process

    response = await sessions_api.session_activity(_request(), runner)
    assert response.media_type == "text/event-stream"
    stream = response.body_iterator
    try:
        snapshot = await _next_data(stream)
        assert snapshot["type"] == "snapshot"
        assert [UUID(item["session_id"]) for item in snapshot["sessions"]] == [mine]

        # Someone else's session finishing is not the caller's business.
        await processes[theirs]._emit_status("idle", reason="prompt_complete")
        await processes[mine]._emit_status("idle", reason="prompt_complete")
        event = await _next_data(stream)
    finally:
        await stream.aclose()  # type: ignore[attr-defined]

    assert event["type"] == "activity"
    assert UUID(event["session_id"]) == mine
    assert event["state"] == "idle"
    assert not runner.activity._subscribers


def _request():
    return SimpleNamespace(query_params={}, headers={}, cookies={})


async def _noop(_message: str) -> None:
    return None


async def _next_data(stream) -> dict:
    async for chunk in stream:
        text = chunk if isinstance(chunk, str) else chunk.decode()
        if text.startswith("data: "):
            return json.loads(text.removeprefix("data: "))
    raise AssertionError("stream ended without data")


def test_the_hub_knows_how_long_no_page_has_been_open() -> None:
    import time

    hub = ActivityHub()
    assert hub.unattended_for() >= 0
    first = hub.subscribe()
    second = hub.subscribe()
    assert hub.unattended_for() == 0
    hub.unsubscribe(first)
    assert hub.unattended_for() == 0  # one page is still open
    hub.unsubscribe(second)
    time.sleep(0.01)
    assert hub.unattended_for() > 0


def _retry_event() -> JSONRPCEventMessage:
    return JSONRPCEventMessage(
        params=StepRetry(n=1, next_attempt=2, max_attempts=0, wait_s=1.0, error_type="X")
    )


async def test_a_retry_marks_the_session_retrying_until_it_makes_progress() -> None:
    from kimo_cli.wire.types import StepBegin

    calls: list[SessionProcess] = []
    process = SessionProcess(uuid4(), on_retry=calls.append)
    process._broadcast = _noop  # type: ignore[assignment]
    await process._emit_status("busy", reason="prompt")

    await process._handle_out_message(_retry_event())
    assert process.is_retrying and calls == [process]

    await process._handle_out_message(JSONRPCEventMessage(params=StepBegin(n=2)))
    assert not process.is_retrying

    await process._handle_out_message(_retry_event())
    await process._emit_status("idle", reason="prompt_error")
    assert not process.is_retrying


@pytest.mark.parametrize(
    ("unattended_s", "expect_cancel"),
    [(None, False), (60.0, False), (200.0, True)],
    ids=["page-open", "closed-one-minute", "closed-past-grace"],
)
async def test_a_retry_nobody_can_stop_is_stopped_after_the_grace(
    unattended_s: float | None, expect_cancel: bool
) -> None:
    import time

    from kimo_cli.web.runner import process as process_module

    runner = KimoCLIRunner()
    page = runner.activity.subscribe()
    if unattended_s is not None:
        runner.activity.unsubscribe(page)
        runner.activity._unattended_since = time.monotonic() - unattended_s
    process = await runner.get_or_create_session(uuid4())
    process._broadcast = _noop  # type: ignore[assignment]
    cancelled: list[str] = []

    async def record_cancel(reason: str) -> None:
        cancelled.append(reason)

    process.cancel_turn = record_cancel  # type: ignore[method-assign]
    await process._emit_status("busy", reason="prompt")

    await process._handle_out_message(_retry_event())
    for _ in range(3):
        await asyncio.sleep(0)

    assert bool(cancelled) is expect_cancel
    assert process_module.UNATTENDED_RETRY_GRACE_S == 180.0


async def test_a_retrying_foreground_subagent_counts_as_the_turn_retrying() -> None:
    from kimo_cli.wire.types import StepBegin, SubagentEvent

    calls: list[SessionProcess] = []
    process = SessionProcess(uuid4(), on_retry=calls.append)
    process._broadcast = _noop  # type: ignore[assignment]
    await process._emit_status("busy", reason="prompt")

    nested = JSONRPCEventMessage(
        params=SubagentEvent(
            parent_tool_call_id="call-1",
            agent_id="a1",
            subagent_type="coder",
            event=StepRetry(n=1, next_attempt=2, max_attempts=0, wait_s=1.0, error_type="X"),
        )
    )
    await process._handle_out_message(nested)
    assert process.is_retrying and calls == [process]

    progress = JSONRPCEventMessage(
        params=SubagentEvent(
            parent_tool_call_id="call-1", agent_id="a1", subagent_type="coder", event=StepBegin(n=2)
        )
    )
    await process._handle_out_message(progress)
    assert not process.is_retrying
