"""Dedicated regression tests for the soul-layer findings."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from kimo_cli.soul import approval as approval_module
from kimo_cli.soul.dynamic_injections import session_memory as session_memory_module
from kimo_cli.soul.slash import add_dir

# ---------------------------------------------------------------------------
# F059 — the MCP config validation has one guard, not two
# ---------------------------------------------------------------------------


def test_load_agent_has_a_single_mcp_config_guard() -> None:
    """The nested ``if mcp_configs:`` was redundant; the validation must stay."""
    from kimo_cli.soul import agent as agent_module

    source = inspect.getsource(agent_module.load_agent)
    assert source.count("if mcp_configs:") == 1


async def test_load_agent_still_rejects_an_invalid_mcp_config(runtime, tmp_path) -> None:
    """The redundant branch was removed without losing the validation."""
    from kimo_cli.exception import MCPConfigError
    from kimo_cli.soul.agent import load_agent

    agent_file = tmp_path / "agent.yaml"
    (tmp_path / "system.md").write_text("prompt", encoding="utf-8")
    agent_file.write_text(
        'version: 1\nagent:\n  name: "Main"\n'
        "  system_prompt_path: ./system.md\n"
        '  tools: ["kimo_cli.tools.think:Think"]\n',
        encoding="utf-8",
    )

    with pytest.raises(MCPConfigError):
        await load_agent(agent_file, runtime, mcp_configs=[{"mcpServers": "not-a-dict"}])


# ---------------------------------------------------------------------------
# F065 — the unused history parameter is acknowledged explicitly
# ---------------------------------------------------------------------------


def test_session_memory_marks_the_unused_history_parameter() -> None:
    """``afk_mode.py`` in the same package sets the precedent."""
    source = inspect.getsource(session_memory_module.SessionMemoryInjectionProvider.get_injections)
    assert "_ = history" in source


async def test_session_memory_injection_still_renders_entries() -> None:
    """The parameter is unused, the behaviour is not."""
    from kimo_cli.memory.entry import MemoryEntry

    entry = MemoryEntry(kind="user", content="the user prefers terse answers")
    soul = SimpleNamespace(
        runtime=SimpleNamespace(
            session=SimpleNamespace(state=SimpleNamespace(session_memory=[entry]))
        )
    )
    injection = session_memory_module.SessionMemoryInjectionProvider()

    injections = await injection.get_injections(history=[], soul=soul)  # type: ignore[arg-type]

    assert len(injections) == 1
    assert "terse" in injections[0].content


# ---------------------------------------------------------------------------
# F067 — add_dir does not re-import what the module already has
# ---------------------------------------------------------------------------


def test_add_dir_does_not_import_kaospath_again() -> None:
    """The module already imports ``KaosPath``; the local import was redundant."""
    source = inspect.getsource(add_dir)
    assert "from kaos.path import KaosPath" not in source


def test_kaospath_is_available_at_module_level() -> None:
    import kimo_cli.soul.slash as slash_module

    assert slash_module.KaosPath is not None


# ---------------------------------------------------------------------------
# F171 — the unused Response alias is gone
# ---------------------------------------------------------------------------


def test_approval_module_has_no_unused_response_alias() -> None:
    """Nothing referenced it; keeping it only invited accidental use."""
    assert not hasattr(approval_module, "Response")
    source = inspect.getsource(approval_module)
    assert "type Response =" not in source


def _unused(_value: Any) -> None:  # pragma: no cover - keeps linters quiet
    return None
