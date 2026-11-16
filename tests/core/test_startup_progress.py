from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

import kimo_cli.app as app_module
import kimo_cli.ui.shell.startup as startup_module
from kimo_cli.app import KimoCLI
from kimo_cli.ui.shell.startup import ShellStartupProgress
from kimo_cli.wiki.context import WIKI_BLOCK_END, WIKI_BLOCK_START


def test_shell_startup_progress_starts_once_and_updates_messages(monkeypatch) -> None:
    events: list[tuple[str, str]] = []

    class FakeStatus:
        def start(self) -> None:
            events.append(("start", ""))

        def update(self, message: str) -> None:
            events.append(("update", message))

        def stop(self) -> None:
            events.append(("stop", ""))

    def fake_status(message: str, *, spinner: str) -> FakeStatus:
        events.append(("create", message))
        assert spinner == "dots"
        return FakeStatus()

    monkeypatch.setattr(startup_module.console, "status", fake_status)

    progress = ShellStartupProgress(enabled=True)
    progress.update("Preparing session...")
    progress.update("Loading agent...")
    progress.stop()

    assert events == [
        ("create", "[cyan]Preparing session...[/cyan]"),
        ("start", ""),
        ("update", "[cyan]Loading agent...[/cyan]"),
        ("stop", ""),
    ]


def test_shell_startup_progress_is_noop_when_disabled(monkeypatch) -> None:
    called = False

    def fake_status(message: str, *, spinner: str):
        nonlocal called
        called = True
        raise AssertionError(f"status() should not be called, got {message!r} {spinner!r}")

    monkeypatch.setattr(startup_module.console, "status", fake_status)

    progress = ShellStartupProgress(enabled=False)
    progress.update("Preparing session...")
    progress.stop()

    assert called is False


@pytest.mark.asyncio
async def test_kimo_cli_create_reports_startup_phases(session, config, monkeypatch) -> None:
    phases: list[str] = []
    fake_runtime = SimpleNamespace(
        session=session,
        config=config,
        llm=None,
        approval=SimpleNamespace(is_yolo=lambda: False, is_afk=lambda: False),
        notifications=SimpleNamespace(recover=lambda: None),
        background_tasks=SimpleNamespace(reconcile=lambda: None),
    )
    fake_agent = SimpleNamespace(name="Test Agent", system_prompt="Test system prompt")
    fake_context = SimpleNamespace(system_prompt=None)
    write_system_prompt = AsyncMock()

    async def fake_runtime_create(*args, **kwargs):
        return fake_runtime

    async def fake_load_agent(*args, **kwargs):
        return fake_agent

    async def fake_restore() -> None:
        return None

    fake_context.restore = fake_restore
    fake_context.write_system_prompt = write_system_prompt

    monkeypatch.setattr(app_module, "load_config", lambda conf: conf)
    monkeypatch.setattr(app_module, "augment_provider_with_env_vars", lambda provider, model: {})
    monkeypatch.setattr(app_module, "create_llm", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module.Runtime, "create", fake_runtime_create)
    monkeypatch.setattr(app_module, "load_agent", fake_load_agent)
    monkeypatch.setattr(app_module, "Context", lambda _path, lease=None: fake_context)

    class _FakeSoul:
        def __init__(self, agent, context):
            pass

        def set_hook_engine(self, engine):
            pass

    monkeypatch.setattr(app_module, "KimoSoul", _FakeSoul)

    cli = await KimoCLI.create(session, config=config, startup_progress=phases.append)

    assert isinstance(cli, KimoCLI)
    assert phases == [
        "Loading configuration...",
        "Scanning workspace...",
        "Loading agent...",
        "Restoring conversation...",
    ]
    write_system_prompt.assert_awaited_once_with("Test system prompt")


@pytest.mark.asyncio
async def test_kimo_cli_create_refreshes_persisted_wiki_block(session, config, monkeypatch) -> None:
    current_context = "Current Wiki guidance.\n\n# Wiki Index\n- [[concepts/current]] — 当前"
    old_prompt = (
        f"Keep before.\n\n{WIKI_BLOCK_START}\n# Global Wiki\nstale\n{WIKI_BLOCK_END}\n\nKeep after."
    )
    close = AsyncMock()
    fake_runtime = SimpleNamespace(
        session=session,
        config=config,
        llm=None,
        approval=SimpleNamespace(is_yolo=lambda: False, is_afk=lambda: False),
        notifications=SimpleNamespace(recover=lambda: None),
        background_tasks=SimpleNamespace(reconcile=lambda: None),
        builtin_args=SimpleNamespace(KIMO_WIKI_CONTEXT=current_context),
        close=close,
    )

    @dataclass
    class _FakeAgent:
        name: str
        system_prompt: str

    fake_agent = _FakeAgent(name="Test Agent", system_prompt="New template")
    fake_context = SimpleNamespace(system_prompt=old_prompt)
    fake_context.restore = AsyncMock()
    fake_context.replace_system_prompt = AsyncMock()
    captured: dict[str, object] = {}

    monkeypatch.setattr(app_module, "load_config", lambda conf: conf)
    monkeypatch.setattr(app_module, "augment_provider_with_env_vars", lambda provider, model: {})
    monkeypatch.setattr(app_module, "create_llm", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module.Runtime, "create", AsyncMock(return_value=fake_runtime))
    monkeypatch.setattr(app_module, "load_agent", AsyncMock(return_value=fake_agent))
    monkeypatch.setattr(app_module, "Context", lambda _path, lease=None: fake_context)

    class _FakeSoul:
        plan_mode = False

        def __init__(self, agent, context):
            captured["prompt"] = agent.system_prompt

        def set_hook_engine(self, engine):
            pass

    monkeypatch.setattr(app_module, "KimoSoul", _FakeSoul)

    await KimoCLI.create(session, config=config, resumed=True)

    prompt = str(captured["prompt"])
    assert "Keep before." in prompt and "Keep after." in prompt
    assert "stale" not in prompt
    assert current_context in prompt
    assert prompt.count(WIKI_BLOCK_START) == 1
    fake_context.replace_system_prompt.assert_awaited_once_with(prompt)
    close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("agent failed"), asyncio.CancelledError()])
async def test_kimo_cli_create_closes_runtime_after_initialization_failure(
    session,
    config,
    monkeypatch,
    failure: BaseException,
) -> None:
    close = AsyncMock()
    fake_runtime = SimpleNamespace(
        session=session,
        config=config,
        llm=None,
        approval=SimpleNamespace(is_yolo=lambda: False, is_afk=lambda: False),
        notifications=SimpleNamespace(recover=lambda: None),
        background_tasks=SimpleNamespace(reconcile=lambda: None),
        builtin_args=SimpleNamespace(KIMO_WIKI_CONTEXT=""),
        close=close,
    )
    monkeypatch.setattr(app_module, "load_config", lambda conf: conf)
    monkeypatch.setattr(app_module, "augment_provider_with_env_vars", lambda provider, model: {})
    monkeypatch.setattr(app_module, "create_llm", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module.Runtime, "create", AsyncMock(return_value=fake_runtime))
    monkeypatch.setattr(app_module, "load_agent", AsyncMock(side_effect=failure))

    with pytest.raises(type(failure)):
        await KimoCLI.create(session, config=config)

    close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_kimo_cli_create_closes_runtime_when_post_runtime_progress_fails(
    session,
    config,
    monkeypatch,
) -> None:
    close = AsyncMock()
    fake_runtime = SimpleNamespace(
        session=session,
        config=config,
        llm=None,
        approval=SimpleNamespace(is_yolo=lambda: False, is_afk=lambda: False),
        notifications=SimpleNamespace(recover=lambda: None),
        background_tasks=SimpleNamespace(reconcile=lambda: None),
        builtin_args=SimpleNamespace(KIMO_WIKI_CONTEXT=""),
        close=close,
    )

    def progress(message: str) -> None:
        if message == "Loading agent...":
            raise RuntimeError("progress failed")

    monkeypatch.setattr(app_module, "load_config", lambda conf: conf)
    monkeypatch.setattr(app_module, "augment_provider_with_env_vars", lambda provider, model: {})
    monkeypatch.setattr(app_module, "create_llm", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module.Runtime, "create", AsyncMock(return_value=fake_runtime))

    with pytest.raises(RuntimeError, match="progress failed"):
        await KimoCLI.create(session, config=config, startup_progress=progress)

    close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_kimo_cli_create_closes_runtime_when_telemetry_setup_fails(
    session,
    config,
    monkeypatch,
) -> None:
    close = AsyncMock()
    fake_runtime = SimpleNamespace(
        session=session,
        config=config,
        llm=None,
        approval=SimpleNamespace(is_yolo=lambda: False, is_afk=lambda: False),
        notifications=SimpleNamespace(recover=lambda: None),
        background_tasks=SimpleNamespace(reconcile=lambda: None),
        builtin_args=SimpleNamespace(KIMO_WIKI_CONTEXT=""),
        close=close,
    )
    fake_agent = SimpleNamespace(name="Test Agent", system_prompt="Test system prompt")
    fake_context = SimpleNamespace(
        system_prompt=None,
        restore=AsyncMock(),
        write_system_prompt=AsyncMock(),
    )

    class _FakeSoul:
        plan_mode = False

        def __init__(self, agent, context):
            pass

        def set_hook_engine(self, engine):
            pass

    monkeypatch.setattr(app_module, "load_config", lambda conf: conf)
    monkeypatch.setattr(app_module, "augment_provider_with_env_vars", lambda provider, model: {})
    monkeypatch.setattr(app_module, "create_llm", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module.Runtime, "create", AsyncMock(return_value=fake_runtime))
    monkeypatch.setattr(app_module, "load_agent", AsyncMock(return_value=fake_agent))
    monkeypatch.setattr(app_module, "Context", lambda _path, lease=None: fake_context)
    monkeypatch.setattr(app_module, "KimoSoul", _FakeSoul)
    monkeypatch.setattr(
        "kimo_cli.telemetry.crash.install_asyncio_handler",
        Mock(side_effect=RuntimeError("telemetry failed")),
    )

    with pytest.raises(RuntimeError, match="telemetry failed"):
        await KimoCLI.create(session, config=config)

    close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_run_shell_does_not_point_people_at_another_product(runtime, monkeypatch) -> None:
    """Upstream's welcome screen urged a move to Kimo Code; OpenKimo's does not."""
    from kimo_cli.ui.shell import WelcomeInfoItem

    captured: dict[str, object] = {}

    class FakeShell:
        def __init__(self, soul, *, welcome_info=None, prefill_text=None) -> None:
            captured["soul"] = soul
            captured["welcome_info"] = list(welcome_info or [])
            captured["prefill_text"] = prefill_text

        async def run(self, command: str | None = None) -> bool:
            captured["command"] = command
            return True

    @contextlib.asynccontextmanager
    async def fake_env():
        yield

    monkeypatch.setattr("kimo_cli.ui.shell.Shell", FakeShell)

    soul = SimpleNamespace(model_name="kimo-cli", name="Kimo Code CLI")
    cli = KimoCLI(soul, runtime, {})  # type: ignore[arg-type]
    monkeypatch.setattr(cli, "_env", fake_env)

    assert await cli.run_shell(command="status") is True

    welcome_info = cast("list[WelcomeInfoItem]", captured["welcome_info"])
    assert all("Update" not in item.name for item in welcome_info)
    assert all("/upgrade" not in str(item.value) for item in welcome_info)


@pytest.mark.asyncio
async def test_kimo_cli_create_cleans_stale_running_foreground_subagents(
    session, config, monkeypatch
) -> None:
    update_instance = Mock()
    fake_runtime = SimpleNamespace(
        session=session,
        config=config,
        llm=None,
        approval=SimpleNamespace(is_yolo=lambda: False, is_afk=lambda: False),
        notifications=SimpleNamespace(recover=lambda: None),
        background_tasks=SimpleNamespace(reconcile=lambda: None),
        subagent_store=SimpleNamespace(
            list_instances=lambda: [
                SimpleNamespace(agent_id="afg1", status="running_foreground"),
                SimpleNamespace(agent_id="abg1", status="running_background"),
                SimpleNamespace(agent_id="aidle1", status="idle"),
            ],
            update_instance=update_instance,
        ),
    )
    fake_agent = SimpleNamespace(name="Test Agent", system_prompt="Test system prompt")
    fake_context = SimpleNamespace(system_prompt=None)
    write_system_prompt = AsyncMock()

    async def fake_runtime_create(*args, **kwargs):
        return fake_runtime

    async def fake_load_agent(*args, **kwargs):
        return fake_agent

    async def fake_restore() -> None:
        return None

    fake_context.restore = fake_restore
    fake_context.write_system_prompt = write_system_prompt

    monkeypatch.setattr(app_module, "load_config", lambda conf: conf)
    monkeypatch.setattr(app_module, "augment_provider_with_env_vars", lambda provider, model: {})
    monkeypatch.setattr(app_module, "create_llm", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module.Runtime, "create", fake_runtime_create)
    monkeypatch.setattr(app_module, "load_agent", fake_load_agent)
    monkeypatch.setattr(app_module, "Context", lambda _path, lease=None: fake_context)

    class _FakeSoul:
        def __init__(self, agent, context):
            pass

        def set_hook_engine(self, engine):
            pass

    monkeypatch.setattr(app_module, "KimoSoul", _FakeSoul)

    await KimoCLI.create(session, config=config)

    update_instance.assert_called_once_with("afg1", status="failed")
