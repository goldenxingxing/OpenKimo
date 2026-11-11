"""Finalization after the step budget is exhausted.

When ``max_steps_per_turn`` runs out, the turn used to end on a bare
``MaxStepsReached`` exception — the user got no answer at all. Now one
no-tools finalization round asks the model to wrap up. Any failure of that
round (error, timeout, or the model still trying to call tools) falls back
to the historical raise.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Self

import pytest
from kosong.chat_provider import StreamedMessagePart, ThinkingEffort, TokenUsage
from kosong.message import Message, TextPart, ToolCall
from kosong.tooling import CallableTool2, Tool, ToolOk, ToolReturnValue
from pydantic import BaseModel

import kimo_cli.soul.kimosoul as kimosoul_module
from kimo_cli.llm import LLM
from kimo_cli.soul.agent import Runtime
from kimo_cli.soul.kimosoul import FINALIZATION_PROMPT, KimoSoul, MaxStepsReached
from kimo_cli.soul.toolset import KimoToolset
from tests.core.test_kimosoul_repeat import _make_soul


class _Params(BaseModel):
    value: str = ""


class _DummyTool(CallableTool2[_Params]):
    name = "ToolA"
    description = "Dummy tool that always succeeds."
    params = _Params

    async def __call__(self, params: _Params) -> ToolReturnValue:
        return ToolOk(output="a")


class _FinalStream:
    def __init__(self, parts: list[StreamedMessagePart]) -> None:
        self._iter = self._to_stream(parts)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        return await self._iter.__anext__()

    async def _to_stream(
        self, parts: list[StreamedMessagePart]
    ) -> AsyncIterator[StreamedMessagePart]:
        for part in parts:
            yield part

    @property
    def id(self) -> str | None:
        return "finalization"

    @property
    def usage(self) -> TokenUsage | None:
        return None

    @property
    def trace_id(self) -> str | None:
        return None


class _FinalizationChatProvider:
    """Burns steps with tool calls while tools exist; a configurable behavior
    when the finalization round arrives (tools list is empty then)."""

    name = "finalization"

    def __init__(
        self,
        on_finalize: str = "text",  # "text" | "tool_call" | "error" | "hang"
    ) -> None:
        self._n = 0
        self._on_finalize = on_finalize
        self.finalize_seen = False

    @property
    def model_name(self) -> str:
        return "finalization"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> _FinalStream:
        if not tools:
            # No tools in the request => this is the finalization round.
            self.finalize_seen = True
            if self._on_finalize == "text":
                return _FinalStream([TextPart(text="completed: X; unfinished: Y; final answer: Z")])
            if self._on_finalize == "tool_call":
                self._n += 1
                return _FinalStream(
                    [
                        ToolCall(
                            id=f"tc-fin-{self._n}",
                            function=ToolCall.FunctionBody(name="ToolA", arguments="{}"),
                        )
                    ]
                )
            if self._on_finalize == "error":
                raise RuntimeError("finalization provider unavailable")
            if self._on_finalize == "hang":
                await asyncio.sleep(3600)
        self._n += 1
        return _FinalStream(
            [
                ToolCall(
                    id=f"tc-{self._n}",
                    function=ToolCall.FunctionBody(name="ToolA", arguments='{"value":"x"}'),
                )
            ]
        )

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


def _soul_with_budget(
    runtime: Runtime,
    provider: _FinalizationChatProvider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    max_steps: int = 3,
    timeout: float = 120.0,
) -> KimoSoul:
    toolset = KimoToolset()
    toolset.add(_DummyTool())
    llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
    soul = _make_soul(runtime, llm, toolset, tmp_path)
    soul._loop_control.max_steps_per_turn = max_steps
    soul._loop_control.finalization_timeout_s = timeout

    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    async def _noop_checkpoint() -> None:
        return None

    monkeypatch.setattr(soul, "_checkpoint", _noop_checkpoint)
    monkeypatch.setattr(soul._denwa_renji, "set_n_checkpoints", lambda _n: None)
    return soul


@pytest.mark.asyncio
async def test_finalization_produces_a_final_answer(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FinalizationChatProvider(on_finalize="text")
    soul = _soul_with_budget(runtime, provider, tmp_path, monkeypatch, max_steps=3)

    outcome = await soul._turn(Message(role="user", content="go"))

    assert provider.finalize_seen
    assert outcome.stop_reason == "max_steps_finalized"
    assert outcome.final_message is not None
    assert "final answer: Z" in outcome.final_message.extract_text("\n")
    # The three burned steps, then the finalization round (not counted).
    assert outcome.step_count == 3


@pytest.mark.asyncio
async def test_finalization_failure_falls_back_to_max_steps_reached(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FinalizationChatProvider(on_finalize="error")
    soul = _soul_with_budget(runtime, provider, tmp_path, monkeypatch)

    with pytest.raises(MaxStepsReached):
        await soul._turn(Message(role="user", content="go"))
    assert provider.finalize_seen


@pytest.mark.asyncio
async def test_finalization_still_calling_tools_falls_back(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FinalizationChatProvider(on_finalize="tool_call")
    soul = _soul_with_budget(runtime, provider, tmp_path, monkeypatch)

    with pytest.raises(MaxStepsReached):
        await soul._turn(Message(role="user", content="go"))
    assert provider.finalize_seen


@pytest.mark.asyncio
async def test_finalization_timeout_falls_back(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FinalizationChatProvider(on_finalize="hang")
    soul = _soul_with_budget(runtime, provider, tmp_path, monkeypatch, timeout=0.5)

    with pytest.raises(MaxStepsReached):
        await soul._turn(Message(role="user", content="go"))
    assert provider.finalize_seen


@pytest.mark.asyncio
async def test_finalization_disabled_keeps_historical_behavior(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FinalizationChatProvider(on_finalize="text")
    soul = _soul_with_budget(runtime, provider, tmp_path, monkeypatch)
    soul._loop_control.finalize_on_max_steps = False

    with pytest.raises(MaxStepsReached):
        await soul._turn(Message(role="user", content="go"))
    assert not provider.finalize_seen


@pytest.mark.asyncio
async def test_finalization_persists_answer_but_not_the_prompt(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FinalizationChatProvider(on_finalize="text")
    soul = _soul_with_budget(runtime, provider, tmp_path, monkeypatch)

    outcome = await soul._turn(Message(role="user", content="go"))
    assert outcome.final_message is not None

    history = soul._context.history
    # The final answer is the last message so resume/replay stays consistent.
    assert "final answer: Z" in history[-1].extract_text("\n")
    # The finalization instruction itself must not pollute the context.
    for message in history:
        assert FINALIZATION_PROMPT not in message.extract_text("\n")
