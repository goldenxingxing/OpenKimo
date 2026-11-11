"""
Test the compensating ToolResult emission in KimoSoul.run()'s finally block.

Root cause: `kosong.step` (packages/kosong/src/kosong/__init__.py) silently
removes the done_callback + cancels the future on Cancel, so a tool_call_id
that made it onto the wire as a ToolCall may never return a ToolResult —
either because the user hit Stop or because the LLM call itself failed.
Consumers that key terminal state off tool_call_id (the web UI's
`subagentRunning`) then hang in an "Agent working…" state for the rest of
the session, even across a resume.

`KimoSoul.run()` wraps `wire_send` inside `_run_step_once` to track issued
vs completed tool_call ids and emits a compensating `ToolResult(is_error)`
for the diff at the end of the turn.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import kosong
import pytest
from kosong.message import ToolCall
from kosong.tooling import ToolError, ToolOk, ToolResult
from kosong.tooling.empty import EmptyToolset

import kimo_cli.soul.kimosoul as kimosoul_module
from kimo_cli.soul.agent import Agent, Runtime
from kimo_cli.soul.approval import Approval
from kimo_cli.soul.context import Context
from kimo_cli.soul.kimosoul import KimoSoul
from kimo_cli.wire.types import TurnEnd


@pytest.fixture
def approval() -> Approval:
    """Override global yolo=True fixture; these tests only need wire semantics."""
    return Approval(yolo=False)


def _make_soul(runtime: Runtime, tmp_path: Path) -> KimoSoul:
    agent = Agent(
        name="Cancel Compensate Agent",
        system_prompt="Test prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    return KimoSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))


@pytest.mark.asyncio
async def test_run_emits_tool_result_for_orphaned_call_on_cancel(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ToolCall emitted to the wire but never returned is compensated on Cancel.

    Mirrors the user-cancelled case from session 8ee7f083: the LLM streams
    a foreground Agent tool call, the user hits Stop, kosong.step raises
    CancelledError without ever delivering the corresponding ToolResult.
    The finally block should emit a compensating ToolResult so downstream
    consumers (the web UI's `subagentRunning`) reach a terminal state.
    """
    soul = _make_soul(runtime, tmp_path)
    sent: list[object] = []

    monkeypatch.setattr(kimosoul_module, "wire_send", lambda msg: sent.append(msg))

    async def fake_step(*_args, **kwargs):
        on_message_part = kwargs["on_message_part"]
        tc = ToolCall(id="call_X", function={"name": "Agent", "arguments": "{}"})
        on_message_part(tc)
        raise asyncio.CancelledError()

    monkeypatch.setattr(kosong, "step", fake_step)

    with pytest.raises(asyncio.CancelledError):
        await soul.run("hello")

    tool_results = [m for m in sent if isinstance(m, ToolResult)]
    assert len(tool_results) == 1, (
        f"expected 1 compensating ToolResult, got {len(tool_results)}: {tool_results}"
    )
    tr = tool_results[0]
    assert tr.tool_call_id == "call_X"
    assert isinstance(tr.return_value, ToolError)
    assert tr.return_value.is_error is True

    # The compensating ToolResult must precede the TurnEnd, so a consumer
    # reading the wire sequentially sees "tool done" before "turn done".
    te_indices = [i for i, m in enumerate(sent) if isinstance(m, TurnEnd)]
    tr_indices = [i for i, m in enumerate(sent) if isinstance(m, ToolResult)]
    assert te_indices, "TurnEnd must still be emitted"
    assert tr_indices[0] < te_indices[0], (
        "compensating ToolResult should precede TurnEnd; wire order was:\n"
        f"{[type(m).__name__ for m in sent]}"
    )


@pytest.mark.asyncio
async def test_run_emits_tool_result_for_each_outstanding_call(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two in-flight calls, one completes, one is orphaned: only the orphaned one is compensated."""
    soul = _make_soul(runtime, tmp_path)
    sent: list[object] = []

    monkeypatch.setattr(kimosoul_module, "wire_send", lambda msg: sent.append(msg))

    async def fake_step(*_args, **kwargs):
        on_message_part = kwargs["on_message_part"]
        on_tool_result = kwargs["on_tool_result"]

        tc_a = ToolCall(id="call_A", function={"name": "Bash", "arguments": "{}"})
        tc_b = ToolCall(id="call_B", function={"name": "Agent", "arguments": "{}"})
        on_message_part(tc_a)
        on_message_part(tc_b)
        # call_A finishes normally; call_B is left in flight.
        on_tool_result(ToolResult(tool_call_id="call_A", return_value=ToolOk(output="ok")))
        raise asyncio.CancelledError()

    monkeypatch.setattr(kosong, "step", fake_step)

    with pytest.raises(asyncio.CancelledError):
        await soul.run("hello")

    trs = [m for m in sent if isinstance(m, ToolResult)]
    ids = [tr.tool_call_id for tr in trs]
    assert "call_A" in ids, "call_A's real ToolResult must be preserved"
    assert "call_B" in ids, "call_B must receive a compensating ToolResult"
    assert ids.count("call_A") == 1, "call_A must not be duplicated"
    assert ids.count("call_B") == 1, "call_B must not be duplicated"

    comp = [tr for tr in trs if tr.tool_call_id == "call_B"][0]
    assert isinstance(comp.return_value, ToolError)
    assert comp.return_value.is_error is True


@pytest.mark.asyncio
async def test_run_does_not_compensate_on_clean_exit_without_tool_calls(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal completion without any ToolCall emits no compensating ToolResult.

    Regression check: on a turn that ends normally (no raise), the diff
    `issued_tool_calls - completed_tool_calls` is empty, so nothing extra
    should be emitted. This mirrors the existing behaviour of a text-only
    reply turn.
    """
    soul = _make_soul(runtime, tmp_path)
    sent: list[object] = []

    monkeypatch.setattr(kimosoul_module, "wire_send", lambda msg: sent.append(msg))

    async def fake_step(*_args, **kwargs):
        # No on_message_part calls, no tool calls at all.
        raise asyncio.CancelledError()

    monkeypatch.setattr(kosong, "step", fake_step)

    with pytest.raises(asyncio.CancelledError):
        await soul.run("hello")

    tool_results = [m for m in sent if isinstance(m, ToolResult)]
    assert tool_results == [], (
        f"no compensating ToolResult expected when no ToolCall was issued, got: {tool_results}"
    )
