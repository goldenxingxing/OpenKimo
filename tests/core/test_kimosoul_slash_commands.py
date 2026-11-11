from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from kaos.path import KaosPath
from kosong.tooling.empty import EmptyToolset

import kimo_cli.soul.kimosoul as kimosoul_module
import kimo_cli.soul.slash as slash_module
from kimo_cli.skill import Skill
from kimo_cli.skill.flow import Flow, FlowEdge, FlowNode
from kimo_cli.soul.agent import Agent, Runtime
from kimo_cli.soul.context import Context
from kimo_cli.soul.kimosoul import KimoSoul
from kimo_cli.utils.slashcmd import SlashCommand
from kimo_cli.wiki.triggers import WikiTurnCoordinator


def _make_flow() -> Flow:
    nodes = {
        "BEGIN": FlowNode(id="BEGIN", label="Begin", kind="begin"),
        "END": FlowNode(id="END", label="End", kind="end"),
    }
    outgoing = {
        "BEGIN": [FlowEdge(src="BEGIN", dst="END", label=None)],
        "END": [],
    }
    return Flow(nodes=nodes, outgoing=outgoing, begin_id="BEGIN", end_id="END")


def test_flow_skill_registers_skill_and_flow_commands(runtime: Runtime, tmp_path: Path) -> None:
    flow = _make_flow()
    skill_dir = tmp_path / "flow-skill"
    skill_dir.mkdir()
    skill_dir_kp = KaosPath.unsafe_from_local_path(skill_dir)
    flow_skill = Skill(
        name="flow-skill",
        description="Flow skill",
        type="flow",
        dir=skill_dir_kp,
        skill_md_file=skill_dir_kp / "SKILL.md",
        flow=flow,
        scope="user",
    )
    runtime.skills = {"flow-skill": flow_skill}

    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = KimoSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))

    command_names = {cmd.name for cmd in soul.available_slash_commands}
    assert "skill:flow-skill" in command_names
    assert "flow:flow-skill" in command_names


@pytest.mark.asyncio
async def test_skill_slash_run_does_not_auto_generate_session_title(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    skill_dir.joinpath("SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: demo-skill",
                "description: Demo skill",
                "---",
                "",
                "Use this skill for tests.",
            ]
        ),
        encoding="utf-8",
    )
    runtime.skills = {
        "demo-skill": Skill(
            name="demo-skill",
            description="Demo skill",
            type="standard",
            dir=KaosPath.unsafe_from_local_path(skill_dir),
            skill_md_file=KaosPath.unsafe_from_local_path(skill_dir / "SKILL.md"),
            scope="user",
        )
    }

    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = KimoSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))
    soul._turn = AsyncMock(return_value=None)  # type: ignore[method-assign]
    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    await soul.run("/skill:demo-skill fix login")

    assert runtime.session.state.custom_title is None


@pytest.mark.asyncio
async def test_flow_slash_run_does_not_auto_generate_session_title(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = KimoSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))
    soul._slash_command_map["flow:demo-flow"] = SlashCommand(
        name="flow:demo-flow",
        description="Demo flow",
        func=lambda *_args, **_kwargs: None,
        aliases=[],
    )
    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    await soul.run("/flow:demo-flow")

    assert runtime.session.state.custom_title is None


@pytest.mark.asyncio
async def test_slash_command_does_not_start_wiki_retrieval(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.wiki_coordinator = WikiTurnCoordinator(provenance_session_id=uuid4())
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = KimoSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))
    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    await soul.run("/flow:demo-flow")

    assert runtime.wiki_coordinator.active_turn_id is None


@pytest.mark.asyncio
async def test_import_slash_without_args_shows_usage_without_importing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    perform_import = AsyncMock()
    sent = []
    monkeypatch.setattr("kimo_cli.utils.export.perform_import", perform_import)
    monkeypatch.setattr(slash_module, "wire_send", sent.append)

    await slash_module.import_context(MagicMock(), "   ")

    perform_import.assert_not_awaited()
    assert sent[0].text == "Usage: /import <file_path or session_id>"
