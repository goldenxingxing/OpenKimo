from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from kosong.tooling.empty import EmptyToolset

import kimo_cli.soul.kimosoul as kimosoul_module
from kimo_cli.soul.agent import Agent, Runtime
from kimo_cli.soul.context import Context
from kimo_cli.soul.kimosoul import KimoSoul
from kimo_cli.wire.types import TurnBegin, TurnEnd


def _soul(runtime: Runtime, tmp_path: Path) -> KimoSoul:
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = KimoSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))
    soul._turn = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return soul


@pytest.mark.asyncio
async def test_a_stopped_first_turn_still_names_the_session(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seen in real sessions: stopped mid-way, then titled "继续"."""
    wire = runtime.session.wire_file
    await wire.append_message(TurnBegin(user_input="/skill:daily-report 写今天的日报"))
    await wire.append_message(TurnBegin(user_input="形成上周的周报"))
    await wire.append_message(TurnEnd())
    await wire.append_message(TurnBegin(user_input="继续"))
    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    await _soul(runtime, tmp_path).run("继续")

    assert runtime.session.state.custom_title == "形成上周的周报"


@pytest.mark.asyncio
async def test_a_subagent_does_not_name_the_session(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seen in a real session: titled with a subagent's task."""
    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    await _soul(replace(runtime, role="subagent"), tmp_path).run(
        "你的任务：核实一批 Obsidian 社区插件能否替代一个自研 python 脚本"
    )

    assert runtime.session.state.custom_title is None
