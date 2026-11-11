"""Regression tests for the wire server's pending-question handling.

A question asked by a turn that died with its worker can never be answered.
The client re-renders it from the session history, the user answers, and the
server drops the answer without a word — so the user reloads and meets the
same question again. The server therefore has to tell the client which
questions are still waiting for an answer.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from kimo_cli.wire.jsonrpc import (
    JSONRPCInitializeMessage,
    JSONRPCRequestMessage,
    JSONRPCSuccessResponse,
)
from kimo_cli.wire.server import WireServer
from kimo_cli.wire.types import QuestionItem, QuestionRequest


def _question(request_id: str = "q-1") -> QuestionRequest:
    return QuestionRequest(
        id=request_id,
        tool_call_id="tc-1",
        questions=[
            QuestionItem(
                question="How should this be handled?",
                header="Approach",
                options=[{"label": "Option A", "description": "the first way"}],
            )
        ],
    )


def _initialize_message() -> JSONRPCInitializeMessage:
    return JSONRPCInitializeMessage(
        id="init-1",
        params=JSONRPCInitializeMessage.Params(
            protocol_version="1",
            capabilities={"supports_question": True},
        ),
    )


def _server() -> WireServer:
    return WireServer(soul=MagicMock())


def _drain(server: WireServer) -> list[object]:
    sent: list[object] = []
    while not server._write_queue.empty():
        sent.append(server._write_queue.get_nowait())
    return sent


@pytest.mark.asyncio
async def test_pending_question_requests_lists_only_unresolved_questions() -> None:
    server = _server()
    pending = _question("q-live")
    server._pending_requests["q-live"] = pending

    assert [request.id for request in server._pending_question_requests()] == ["q-live"]

    # A resolved question is no longer waiting for an answer.
    pending.resolve({"How should this be handled?": "Option A"})
    assert server._pending_question_requests() == []


def test_pending_question_requests_ignores_other_request_kinds() -> None:
    server = _server()
    server._pending_requests["other"] = MagicMock()

    assert server._pending_question_requests() == []


@pytest.mark.asyncio
async def test_initialize_reports_the_questions_that_can_still_be_answered() -> None:
    server = _server()
    server._pending_requests["q-live"] = _question("q-live")

    await server._dispatch_msg(_initialize_message())

    init_response = next(
        msg
        for msg in _drain(server)
        if isinstance(msg, JSONRPCSuccessResponse) and msg.id == "init-1"
    )
    result = init_response.result
    assert isinstance(result, dict)
    question_requests = result["question_requests"]
    assert [entry["payload"]["id"] for entry in question_requests] == ["q-live"]


@pytest.mark.asyncio
async def test_initialize_resends_the_pending_question_to_the_client() -> None:
    """The client re-renders the history on every connect; only a re-sent
    question is proof that it is still answerable."""
    server = _server()
    server._pending_requests["q-live"] = _question("q-live")

    await server._dispatch_msg(_initialize_message())

    resent = [msg for msg in _drain(server) if isinstance(msg, JSONRPCRequestMessage)]
    assert [msg.id for msg in resent] == ["q-live"]
    assert isinstance(resent[0].params, QuestionRequest)


@pytest.mark.asyncio
async def test_initialize_does_not_resend_a_question_that_is_gone() -> None:
    """A question whose worker died is not in the pending set, so nothing is
    re-sent and the client leaves it closed."""
    server = _server()

    await server._dispatch_msg(_initialize_message())

    assert [msg for msg in _drain(server) if isinstance(msg, JSONRPCRequestMessage)] == []


@pytest.mark.asyncio
async def test_finished_dispatch_tasks_do_not_accumulate() -> None:
    """The dispatch task set is walked by _shutdown; keeping every finished
    task in it grew the set with every message the session ever received."""
    server = _server()

    async def _boom() -> None:
        raise RuntimeError("dispatch failed")

    task = asyncio.create_task(_boom())
    server._dispatch_tasks.add(task)
    await asyncio.sleep(0)  # let it fail

    server._dispatch_task_done(task)

    assert task not in server._dispatch_tasks
