from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import acp
import pytest
from kaos.path import KaosPath

from kimo_cli.acp.kaos import ACPProcess
from kimo_cli.acp.server import ACPServer, _ModelIDConv
from kimo_cli.acp.session import ACPSession
from kimo_cli.acp.tools import Terminal
from kimo_cli.config import LLMModel
from kimo_cli.tools.shell import Params as ShellParams
from kimo_cli.tools.shell import Shell

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("exit_code", [0, 1, 7])
async def test_acp_process_preserves_exit_code_including_zero(exit_code: int) -> None:
    """``exit_code or fallback`` turned a legitimate 0 into the fallback (1)."""

    class _Client:
        async def terminal_output(self, session_id: str, terminal_id: str) -> object:
            # exit_status present but its exit_code is null → the protocol-level
            # exit code must win, even when it is 0.
            return SimpleNamespace(
                output="", truncated=False, exit_status=SimpleNamespace(exit_code=None)
            )

        async def wait_for_terminal_exit(self, session_id: str, terminal_id: str) -> object:
            return SimpleNamespace(exit_code=exit_code)

    process = ACPProcess(_Client(), "session", "terminal", poll_interval=0)
    assert await asyncio.wait_for(process.wait(), timeout=2) == exit_code


async def test_terminal_delegates_background_commands_to_shell() -> None:
    shell = AsyncMock(spec=Shell)
    shell.name = "Shell"
    shell.description = "Run commands"
    shell.params = ShellParams
    expected = Mock()
    shell.return_value = expected
    conn = Mock()
    terminal = Terminal(shell, conn, "session", Mock())

    result = await terminal(
        ShellParams(command="sleep 10", run_in_background=True, description="sleeper")
    )

    assert result is expected
    shell.assert_awaited_once()
    conn.create_terminal.assert_not_called()


class _FakeCLI:
    def __init__(self, config: object | None = None) -> None:
        self.soul = SimpleNamespace(
            runtime=SimpleNamespace(config=config),
            agent=SimpleNamespace(toolset=object()),
        )
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _FailingWireFile:
    path = Path("wire.jsonl")

    async def iter_records(self):
        raise RuntimeError("bad history")
        yield  # pragma: no cover


async def test_replay_failure_cancels_followup_and_closes_cli() -> None:
    cli = _FakeCLI()
    session = ACPSession("session", cli, Mock())  # type: ignore[arg-type]
    followup = asyncio.create_task(asyncio.Event().wait())
    session._followup_task = followup

    with pytest.raises(RuntimeError, match="bad history"):
        await session.replay_history(_FailingWireFile())  # type: ignore[arg-type]

    assert followup.cancelled()
    assert cli.close_calls == 1


async def test_concurrent_resume_closes_replaced_session(tmp_path: Path) -> None:
    server = ACPServer()
    server.conn = Mock()
    server.client_capabilities = acp.schema.ClientCapabilities()
    model = LLMModel(provider="provider", model="model", max_context_size=1000)
    config = SimpleNamespace(default_model="model", default_thinking=False, models={"model": model})
    persisted = SimpleNamespace(id="session")
    clients: list[_FakeCLI] = []

    async def create_cli(*args, **kwargs):
        await asyncio.sleep(0)
        cli = _FakeCLI(config)
        clients.append(cli)
        return cli

    with (
        patch.object(ACPServer, "_check_token_usable", return_value=None),
        patch("kimo_cli.acp.server.Session.find", AsyncMock(return_value=persisted)),
        patch("kimo_cli.acp.server.KimoCLI.create", create_cli),
    ):
        await asyncio.gather(
            server.resume_session(str(tmp_path), "session"),
            server.resume_session(str(tmp_path), "session"),
        )

    assert len(clients) == 2
    assert sorted(cli.close_calls for cli in clients) == [0, 1]
    await server.sessions["session"][0].close()


async def test_list_sessions_includes_and_refreshes_active_empty_session(tmp_path: Path) -> None:
    server = ACPServer()
    work_dir = KaosPath.unsafe_from_local_path(tmp_path)
    active = SimpleNamespace(
        id="active",
        work_dir=work_dir,
        title="stale",
        updated_at=0.0,
        refresh=AsyncMock(),
    )
    server.sessions["active"] = (
        SimpleNamespace(cli=SimpleNamespace(session=active)),
        _ModelIDConv("model", False),
    )  # type: ignore[assignment]

    with patch("kimo_cli.acp.server.Session.list", AsyncMock(return_value=[])):
        response = await server.list_sessions(cwd=str(tmp_path))

    assert [item.session_id for item in response.sessions] == ["active"]
    active.refresh.assert_awaited_once_with()


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


async def test_unknown_model_does_not_switch_runtime_or_save() -> None:
    server, runtime, _ = _model_server()
    old_llm = runtime.llm

    with (
        patch("kimo_cli.acp.server.save_config") as save,
        pytest.raises(acp.RequestError),
    ):
        await server.set_session_model("missing", "session")

    assert runtime.llm is old_llm
    save.assert_not_called()


async def test_model_change_saves_a_fresh_config_not_runtime_object() -> None:
    server, runtime, config = _model_server()
    fresh = SimpleNamespace(default_model="old", default_thinking=False)
    new_llm = object()

    with (
        patch("kimo_cli.acp.server.create_llm", return_value=new_llm),
        patch("kimo_cli.acp.server.load_config", return_value=fresh),
        patch("kimo_cli.acp.server.save_config") as save,
    ):
        await server.set_session_model("new,thinking", "session")

    assert runtime.llm is new_llm
    assert (config.default_model, config.default_thinking) == ("new", True)
    save.assert_called_once_with(fresh)
    assert fresh is not config
    assert (fresh.default_model, fresh.default_thinking) == ("new", True)


async def test_auth_required_payload_is_json_serializable() -> None:
    """`authMethods` must be plain dicts.

    `_auth_methods` holds `acp.schema.AuthMethod` models; passing them through in
    the error payload makes the JSON-RPC encoder raise TypeError, so the client
    gets no response at all instead of AUTH_REQUIRED.
    """
    server = ACPServer()
    server._auth_methods = [
        acp.schema.AuthMethod(
            id="login",
            name="Login",
            description="Run `kimo login`",
            field_meta={"terminal-auth": {"type": "terminal", "args": ["login"], "env": {}}},
        )
    ]
    expected = [
        {
            "id": "login",
            "name": "Login",
            "description": "Run `kimo login`",
            "type": "terminal",
            "args": ["login"],
            "env": {},
        }
    ]

    with patch.object(ACPServer, "_check_token_usable", return_value="no valid token"):
        with pytest.raises(acp.RequestError) as from_authenticate:
            await server.authenticate("login")
        with pytest.raises(acp.RequestError) as from_check_auth:
            server._check_auth()

    for excinfo, key in ((from_authenticate, "authMethods"), (from_check_auth, "authMethods")):
        # Round-tripping through json is the actual contract the wire layer needs.
        decoded = json.loads(json.dumps(excinfo.value.to_error_obj()))
        assert decoded["data"][key] == expected


class _PreprocessingShell:
    """Shell stand-in: only what Terminal touches on the foreground path."""

    name = "Shell"
    description = "Run commands"
    params = ShellParams

    def __init__(self) -> None:
        self.seen: list[str] = []

    def preprocess_command(self, command: str) -> str:
        self.seen.append(command)
        return command.replace(">nul", ">/dev/null")

    async def __call__(self, params: ShellParams) -> object:  # pragma: no cover
        raise AssertionError("background path is not exercised here")


async def test_acp_terminal_runs_the_preprocessed_command() -> None:
    """The ACP terminal must send the same rewritten command local execution runs."""
    shell = _PreprocessingShell()
    conn = Mock()
    conn.create_terminal = AsyncMock(side_effect=RuntimeError("stop after capture"))
    approval = Mock()
    approval.request = AsyncMock(return_value=Mock())

    terminal = Terminal(shell, conn, "session", approval)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="stop after capture"):
        await terminal(ShellParams(command="echo hi >nul"))

    assert shell.seen == ["echo hi >nul"]
    assert conn.create_terminal.await_args.kwargs["command"] == "echo hi >/dev/null"


async def test_new_session_failure_leaves_no_half_configured_entry(tmp_path: Path) -> None:
    """A failing replace_tools must not leave a registered, unusable session."""
    server = ACPServer()
    server.conn = Mock()
    server.client_capabilities = acp.schema.ClientCapabilities()

    class _Toolset:
        """Stand-in for KimoToolset so the replace_tools branch is taken."""

    cli = _FakeCLI(SimpleNamespace(default_model="m", default_thinking=False, models={}))
    cli.soul.agent = SimpleNamespace(toolset=_Toolset())

    with (
        patch.object(ACPServer, "_check_token_usable", return_value=None),
        patch(
            "kimo_cli.acp.server.Session.create",
            AsyncMock(return_value=SimpleNamespace(id="sess")),
        ),
        patch("kimo_cli.acp.server.KimoCLI.create", AsyncMock(return_value=cli)),
        patch("kimo_cli.acp.server.KimoToolset", _Toolset),
        patch("kimo_cli.acp.server.replace_tools", side_effect=RuntimeError("bad toolset")),
        pytest.raises(RuntimeError, match="bad toolset"),
    ):
        await server.new_session(str(tmp_path))

    assert server.sessions == {}
    assert cli.close_calls == 1


async def test_load_session_keeps_replay_error_when_close_also_fails(tmp_path: Path) -> None:
    """Closing after a failed replay must not mask the original exception."""

    class _BadSession:
        cli = SimpleNamespace(
            soul=SimpleNamespace(runtime=SimpleNamespace(session=SimpleNamespace(wire_file=None)))
        )

        async def replay_history(self, wire_file: object) -> None:
            raise RuntimeError("replay broke")

        async def close(self) -> None:
            raise RuntimeError("close broke")

    server = ACPServer()
    bad = _BadSession()

    with (
        patch.object(ACPServer, "_check_token_usable", return_value=None),
        patch.object(
            ACPServer,
            "_setup_session",
            AsyncMock(return_value=(bad, _ModelIDConv("m", False))),
        ),
        pytest.raises(RuntimeError, match="replay broke"),
    ):
        await server.load_session(str(tmp_path), "sess")

    assert "sess" not in server.sessions


async def test_resume_session_requires_auth(tmp_path: Path) -> None:
    """resume_session must apply the same gate as new_session/load_session."""
    server = ACPServer()
    server.conn = Mock()
    server.client_capabilities = acp.schema.ClientCapabilities()

    with (
        patch.object(ACPServer, "_check_token_usable", return_value="no valid token"),
        patch.object(ACPServer, "_setup_session", AsyncMock()) as setup,
        pytest.raises(acp.RequestError),
    ):
        await server.resume_session(str(tmp_path), "sess")

    setup.assert_not_awaited()


async def test_list_sessions_reports_absolute_cwd() -> None:
    """SessionInfo.cwd must be absolute per the ACP schema, whatever the client sent."""
    server = ACPServer()
    entry = SimpleNamespace(
        id="a",
        work_dir=KaosPath.unsafe_from_local_path(Path(".")),
        title="t",
        updated_at=1.0,
    )

    with patch("kimo_cli.acp.server.Session.list", AsyncMock(return_value=[entry])):
        response = await server.list_sessions(cwd=".")

    reported = Path(response.sessions[0].cwd)
    assert reported.is_absolute()


async def test_set_session_mode_rejects_unknown_mode() -> None:
    server = ACPServer()

    with pytest.raises(acp.RequestError):
        await server.set_session_mode("compact", "sess")

    await server.set_session_mode("default", "sess")


# ---------------------------------------------------------------------------
# F007 — the parallel terminal-output hiding mechanism is gone
# ---------------------------------------------------------------------------


async def test_acp_session_has_no_parallel_terminal_hiding_mechanism() -> None:
    """Only ``HideOutputDisplayBlock`` is live; the ContextVar path was dead."""
    from kimo_cli.acp import session as acp_session

    assert not hasattr(acp_session, "_terminal_tool_call_ids")
    assert not hasattr(acp_session, "register_terminal_tool_call_id")
    assert not hasattr(acp_session, "should_hide_terminal_output")


async def test_terminal_output_is_still_hidden_through_the_display_block() -> None:
    """The live path must keep working after the dead one was removed."""
    from kosong.tooling import ToolReturnValue

    from kimo_cli.acp.convert import tool_result_to_acp_content
    from kimo_cli.acp.tools import HideOutputDisplayBlock

    result = ToolReturnValue(
        is_error=False,
        output="terminal output",
        message="done",
        display=[HideOutputDisplayBlock()],
    )

    assert tool_result_to_acp_content(result) == []


# ---------------------------------------------------------------------------
# F008 — the follow-up failure warning is logged once, not every poll
# ---------------------------------------------------------------------------


async def test_follow_up_failure_warning_is_logged_once() -> None:
    """The branch is re-entered on every poll; warning there floods the log."""
    from kimo_cli import logger as kimo_logger
    from kimo_cli.acp.session import ACPSession
    from kimo_cli.soul.kimosoul import KimoSoul

    completion_event = asyncio.Event()

    class _Manager:
        def __init__(self) -> None:
            self.completion_event = completion_event

        def reconcile(self) -> None:
            return None

    class _Notifications:
        def has_pending_for_sink(self, _sink: str) -> bool:
            return True

    class _Background:
        auto_followup_coalesce_ms = 0
        wait_poll_interval_ms = 10

    class _Config:
        background = _Background()

    class _Runtime:
        background_tasks = _Manager()
        notifications = _Notifications()
        config = _Config()

    class _Soul(KimoSoul):
        @property
        def runtime(self):  # type: ignore[override]
            return _Runtime()

    soul = _Soul.__new__(_Soul)

    session = object.__new__(ACPSession)
    session._cli = SimpleNamespace(soul=soul)  # type: ignore[attr-defined]
    session._followup_armed = True
    session._turn_state = None
    session._followup_failures = ACPSession._MAX_FOLLOWUP_FAILURES
    session._followup_disabled = False

    records: list[str] = []
    kimo_logger.enable("kimo_cli")
    sink_id = kimo_logger.add(lambda message: records.append(str(message)), level="WARNING")
    try:
        task = asyncio.create_task(session._background_followup_loop())
        await asyncio.sleep(0.4)  # several poll cycles
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        kimo_logger.remove(sink_id)

    warnings = [r for r in records if "auto-follow-up disabled" in r]
    assert len(warnings) == 1
    assert session._followup_disabled is True


# ---------------------------------------------------------------------------
# F174 — the terminal-auth payload carries exactly the login argument
# ---------------------------------------------------------------------------


async def test_terminal_auth_payload_carries_only_the_login_argument() -> None:
    """The ``args`` list used to be built and then never modified."""
    from kimo_cli.acp.server import ACPServer

    server = object.__new__(ACPServer)
    server._auth_methods = [
        acp.schema.AuthMethod(
            id="login",
            name="Login with Kimo account",
            description="d",
            field_meta={
                "terminal-auth": {
                    "command": "kimo",
                    "args": ["login"],
                    "label": "Kimo Login",
                    "env": {},
                    "type": "terminal",
                }
            },
        )
    ]

    payload = ACPServer._auth_methods_payload(server)

    assert payload[0]["args"] == ["login"]
    assert payload[0]["type"] == "terminal"


# ---------------------------------------------------------------------------
# F177 — the always-thinking verdict comes from the shared capability helper
# ---------------------------------------------------------------------------


async def test_expand_llm_models_marks_always_thinking_models() -> None:
    """The name heuristic lives in ``llm.py``; ACP must not duplicate it."""
    from kimo_cli.acp.server import _expand_llm_models

    models = {
        "vendor/always": LLMModel(provider="p", model="kimo-thinking-preview", max_context_size=1),
        "vendor/plain": LLMModel(provider="p", model="plain-model", max_context_size=1),
    }

    expanded = _expand_llm_models(models)
    ids = [str(entry.model_id) for entry in expanded]

    assert "vendor/always,thinking" in ids
    assert "vendor/plain" in ids
