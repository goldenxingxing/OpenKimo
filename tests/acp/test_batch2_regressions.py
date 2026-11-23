from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import acp
import pytest
from kosong.message import ToolCall
from kosong.tooling import ToolError, ToolResult

from kimo_cli.acp.kaos import ACPProcess
from kimo_cli.acp.server import ACPServer, _ModelIDConv
from kimo_cli.acp.session import ACPSession
from kimo_cli.config import LLMModel
from kimo_cli.wire.types import StepInterrupted, TurnBegin, TurnEnd

pytestmark = pytest.mark.asyncio


async def test_truncated_snapshots_emit_only_their_unseen_suffix() -> None:
    process = object.__new__(ACPProcess)
    process._stdout = asyncio.StreamReader()
    process._last_output = ""
    process._truncation_noted = False

    snapshots = [
        ("0123456789", True),
        ("0123456789", True),
        ("456789abcd", True),
        ("456789abcd", True),
    ]
    for output, truncated in snapshots:
        process._feed_output(SimpleNamespace(output=output, truncated=truncated))  # type: ignore[arg-type]

    expected = "[acp output truncated]\n0123456789abcd"
    actual = await process._stdout.readexactly(len(expected.encode()))
    assert actual.decode() == expected


async def test_poll_failure_completes_process_and_releases_terminal() -> None:
    class _Client:
        def __init__(self) -> None:
            self.release_terminal = AsyncMock()

        async def terminal_output(self, session_id: str, terminal_id: str) -> object:
            raise RuntimeError("poll failed")

        async def wait_for_terminal_exit(self, session_id: str, terminal_id: str) -> object:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    client = _Client()
    process = ACPProcess(client, "session", "terminal", poll_interval=0)  # type: ignore[arg-type]

    assert await asyncio.wait_for(process.wait(), timeout=1) == 1
    await process._poll_task

    assert process.returncode == 1
    assert b"poll failed" in await process._stdout.read()
    assert process._stdout.at_eof()
    assert process._stderr.at_eof()
    client.release_terminal.assert_awaited_once_with(session_id="session", terminal_id="terminal")


async def test_cancelled_waiter_does_not_cancel_shared_exit_future() -> None:
    exited = asyncio.Event()

    class _Client:
        async def terminal_output(self, session_id: str, terminal_id: str) -> object:
            return SimpleNamespace(output="", truncated=False, exit_status=None)

        async def wait_for_terminal_exit(self, session_id: str, terminal_id: str) -> object:
            await exited.wait()
            return SimpleNamespace(exit_code=7)

        async def release_terminal(self, session_id: str, terminal_id: str) -> None:
            return None

    process = ACPProcess(_Client(), "session", "terminal", poll_interval=0.001)  # type: ignore[arg-type]

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=0.01)
    assert not process._exit_future.cancelled()

    exited.set()
    assert await asyncio.wait_for(process.wait(), timeout=1) == 7
    await process._poll_task


class _FakeCLI:
    def __init__(self, config: object) -> None:
        self.soul = SimpleNamespace(
            runtime=SimpleNamespace(
                config=config,
                session=SimpleNamespace(wire_file=object()),
            ),
            agent=SimpleNamespace(toolset=object()),
        )
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


async def test_concurrent_load_sets_up_and_replays_once(tmp_path: Path) -> None:
    server = ACPServer()
    server.conn = Mock()
    server.client_capabilities = acp.schema.ClientCapabilities()
    model = LLMModel(provider="provider", model="model", max_context_size=1000)
    config = SimpleNamespace(default_model="model", default_thinking=False, models={"model": model})
    clients: list[_FakeCLI] = []
    replay_calls = 0

    async def create_cli(*args: object, **kwargs: object) -> _FakeCLI:
        await asyncio.sleep(0)
        cli = _FakeCLI(config)
        clients.append(cli)
        return cli

    async def replay(self: ACPSession, wire_file: object) -> None:
        nonlocal replay_calls
        replay_calls += 1
        await asyncio.sleep(0)

    with (
        patch.object(ACPServer, "_check_token_usable", return_value=None),
        patch(
            "kimo_cli.acp.server.Session.find",
            AsyncMock(return_value=SimpleNamespace(id="session")),
        ),
        patch("kimo_cli.acp.server.KimoCLI.create", create_cli),
        patch.object(ACPSession, "replay_history", replay),
    ):
        await asyncio.gather(
            server.load_session(str(tmp_path), "session"),
            server.load_session(str(tmp_path), "session"),
        )

    assert len(clients) == 1
    assert replay_calls == 1
    assert clients[0].close_calls == 0
    await server.sessions["session"][0].close()


def _model_server() -> tuple[ACPServer, SimpleNamespace, SimpleNamespace]:
    server = ACPServer()
    old_model = LLMModel(provider="provider", model="old", max_context_size=1000)
    new_model = LLMModel(provider="provider", model="new", max_context_size=1000)
    config = SimpleNamespace(
        models={"old": old_model, "new": new_model},
        providers={"provider": object()},
        default_model="old",
        default_thinking=False,
        is_from_default_location=True,
    )
    runtime = SimpleNamespace(config=config, llm=object(), oauth=None)
    cli = SimpleNamespace(soul=SimpleNamespace(runtime=runtime))
    acp_session = SimpleNamespace(id="session", cli=cli)
    server.sessions["session"] = (acp_session, _ModelIDConv("old", False))  # type: ignore[assignment]
    return server, runtime, config


async def test_none_model_leaves_runtime_registry_and_config_untouched() -> None:
    server, runtime, config = _model_server()
    old_llm = runtime.llm

    with (
        patch("kimo_cli.acp.server.create_llm", return_value=None),
        patch("kimo_cli.acp.server.load_config") as load,
        patch("kimo_cli.acp.server.save_config") as save,
        pytest.raises(acp.RequestError),
    ):
        await server.set_session_model("new", "session")

    assert runtime.llm is old_llm
    assert server.sessions["session"][1] == _ModelIDConv("old", False)
    assert (config.default_model, config.default_thinking) == ("old", False)
    load.assert_not_called()
    save.assert_not_called()


def _tool_call() -> ToolCall:
    return ToolCall(
        id="call",
        function=ToolCall.FunctionBody(name="Shell", arguments="{}"),
    )


def _compensating_result() -> ToolResult:
    return ToolResult(
        tool_call_id="call",
        return_value=ToolError(message="interrupted", brief="interrupted"),
    )


def _updates(conn: Mock) -> list[object]:
    return [call.kwargs["update"] for call in conn.session_update.await_args_list]


async def test_interrupted_live_turn_drains_compensation_and_turn_end() -> None:
    turn_end_seen = False

    async def run(prompt: object, cancel_event: asyncio.Event):
        nonlocal turn_end_seen
        yield TurnBegin(user_input="run")
        yield _tool_call()
        yield StepInterrupted()
        yield _compensating_result()
        yield TurnEnd(interrupt_reason="error")
        turn_end_seen = True
        raise RuntimeError("step failed")

    cli = SimpleNamespace(run=run, close=AsyncMock())
    conn = Mock()
    conn.session_update = AsyncMock()
    session = ACPSession("session", cli, conn)  # type: ignore[arg-type]

    with pytest.raises(acp.RequestError):
        await session.prompt([])

    assert turn_end_seen
    assert any(
        isinstance(update, acp.schema.ToolCallProgress) and update.status == "failed"
        for update in _updates(conn)
    )


class _WireFile:
    path = Path("wire.jsonl")

    def __init__(self, messages: list[object]) -> None:
        self.messages = messages

    async def iter_records(self):
        for message in self.messages:
            yield SimpleNamespace(to_wire_message=lambda message=message: message)


async def test_interrupted_replay_keeps_state_for_compensating_result() -> None:
    wire_file = _WireFile(
        [
            TurnBegin(user_input="run"),
            _tool_call(),
            StepInterrupted(),
            _compensating_result(),
            TurnEnd(interrupt_reason="error"),
        ]
    )
    cli = SimpleNamespace(close=AsyncMock())
    conn = Mock()
    conn.session_update = AsyncMock()
    session = ACPSession("session", cli, conn)  # type: ignore[arg-type]

    await session.replay_history(wire_file)  # type: ignore[arg-type]

    assert any(
        isinstance(update, acp.schema.ToolCallProgress) and update.status == "failed"
        for update in _updates(conn)
    )
    assert session._turn_state is None
