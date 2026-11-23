"""Two attached sockets cannot both pass the idle check for one session."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from kimo_cli.web.api import sessions as sessions_api
from kimo_cli.wire.jsonrpc import JSONRPCPromptMessage

if TYPE_CHECKING:
    from starlette.websockets import WebSocket


class _Socket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_text(self, message: str) -> None:
        self.messages.append(message)


class _Process:
    def __init__(self) -> None:
        self.session_id = uuid4()
        self.status = SimpleNamespace(state="idle")
        self.in_flight: set[str] = set()
        self.sent: list[str] = []
        self.first_send_started = asyncio.Event()
        self.release_first_send = asyncio.Event()

    @property
    def is_busy(self) -> bool:
        return bool(self.in_flight)

    def clear_in_flight(self) -> None:
        self.in_flight.clear()

    async def send_message(self, message: str) -> None:
        self.sent.append(message)
        self.in_flight.add(json.loads(message)["id"])
        if len(self.sent) == 1:
            self.first_send_started.set()
            await self.release_first_send.wait()


def _prompt(prompt_id: str) -> tuple[JSONRPCPromptMessage, str]:
    message = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "prompt",
            "id": prompt_id,
            "params": {"user_input": prompt_id},
        }
    )
    return JSONRPCPromptMessage.model_validate_json(message), message


@pytest.mark.anyio
async def test_second_websocket_prompt_waits_for_handoff_then_sees_busy() -> None:
    process = _Process()
    first_socket = _Socket()
    second_socket = _Socket()
    first_message, first_raw = _prompt("first")
    second_message, second_raw = _prompt("second")

    first = asyncio.create_task(
        sessions_api._send_session_prompt(
            process,
            cast("WebSocket", first_socket),
            first_message,
            first_raw,
        )
    )
    await process.first_send_started.wait()
    second = asyncio.create_task(
        sessions_api._send_session_prompt(
            process,
            cast("WebSocket", second_socket),
            second_message,
            second_raw,
        )
    )
    await asyncio.sleep(0)
    assert not second.done(), "the second connection crossed the first send handoff"

    process.release_first_send.set()
    assert await first is True
    assert await second is False

    assert process.sent == [first_raw]
    assert first_socket.messages == []
    assert len(second_socket.messages) == 1
    error = json.loads(second_socket.messages[0])
    assert error["id"] == "second"
    assert error["error"]["message"].startswith("Session is busy")
