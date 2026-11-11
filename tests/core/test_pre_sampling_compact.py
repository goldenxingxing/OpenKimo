"""Pre-sampling compaction and tool-schema budget in the compaction trigger.

Two behaviours under test:

1. The compaction check runs BEFORE the incoming user message is appended
   (codex's ``run_pre_sampling_compact``), counting the new message's own
   tokens/bytes — so a near-full window plus a fresh prompt compacts first
   instead of overshooting inside the first step.
2. The trigger counts tool schemas too: message history alone undercounts
   the request the provider actually sees (zeroclaw budgets schema tokens
   for the same reason).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Self

import pytest
from kosong.chat_provider import StreamedMessagePart, ThinkingEffort, TokenUsage
from kosong.message import Message, TextPart
from kosong.tooling import CallableTool2, Tool, ToolOk, ToolReturnValue
from pydantic import BaseModel

import kimo_cli.soul.kimosoul as kimosoul_module
from kimo_cli.config import LoopControl
from kimo_cli.llm import LLM
from kimo_cli.soul.agent import Runtime
from kimo_cli.soul.compaction import COMPACTION_SYSTEM_PROMPT
from kimo_cli.soul.toolset import KimoToolset
from tests.core.test_kimosoul_repeat import _make_soul


class _Stream:
    def __init__(self, text: str) -> None:
        self._text = text
        self._iter = self._parts()

    async def _parts(self) -> AsyncIterator[StreamedMessagePart]:
        yield TextPart(text=self._text)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        return await self._iter.__anext__()

    @property
    def id(self) -> str | None:
        return None

    @property
    def usage(self) -> TokenUsage | None:
        return None


class _RecordingProvider:
    """Records every request in order; compaction requests get a summary."""

    name = "recording"

    def __init__(self) -> None:
        # Each entry: (is_compaction, history_texts_at_call_time)
        self.calls: list[tuple[bool, list[str]]] = []

    @property
    def model_name(self) -> str:
        return "recording"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self, system_prompt: str, tools: Sequence[Tool], history: Sequence[Message]
    ) -> _Stream:
        texts = [
            part.text
            for message in history
            for part in message.content
            if isinstance(part, TextPart) and isinstance(part.text, str)
        ]
        self.calls.append((system_prompt == COMPACTION_SYSTEM_PROMPT, texts))
        return _Stream("summary" if system_prompt == COMPACTION_SYSTEM_PROMPT else "done")

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class _Params(BaseModel):
    value: str = ""


class _SmallTool(CallableTool2[_Params]):
    name = "SmallTool"
    description = "Tiny."
    params = _Params

    async def __call__(self, params: _Params) -> ToolReturnValue:
        return ToolOk(output="ok")


class _FatTool(CallableTool2[_Params]):
    name = "FatTool"
    # ~2000 tokens of schema by description alone (ascii chars / 4).
    description = "x" * 8000
    params = _Params

    async def __call__(self, params: _Params) -> ToolReturnValue:
        return ToolOk(output="ok")


def _tight_loop_control() -> LoopControl:
    # Window 10000 tokens; ratio trigger at 5000, reserved trigger at 9000 —
    # the ratio condition is the one meant to fire in these tests.
    return LoopControl(
        compaction_trigger_ratio=0.5,
        reserved_context_size=1000,
        prune_tool_outputs=False,
    )


async def _seed_history(soul, total_chars: int) -> None:
    """Append alternating messages until the estimated history is ~total_chars."""
    per_message = 200
    n = max(4, total_chars // per_message)
    for i in range(n):
        await soul._context.append_message(
            Message(role="user" if i % 2 == 0 else "assistant", content="y" * per_message)
        )


@pytest.mark.asyncio
async def test_compaction_fires_before_new_message_enters_history(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """History near the threshold + fresh prompt: compact first, append after."""
    provider = _RecordingProvider()
    toolset = KimoToolset()
    toolset.add(_SmallTool())
    llm = LLM(chat_provider=provider, max_context_size=10000, capabilities=set())
    soul = _make_soul(runtime, llm, toolset, tmp_path)
    soul._loop_control = _tight_loop_control()

    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    async def _noop_checkpoint() -> None:
        return None

    monkeypatch.setattr(soul, "_checkpoint", _noop_checkpoint)
    monkeypatch.setattr(soul._denwa_renji, "set_n_checkpoints", lambda _n: None)

    # ~18000 chars ≈ 4500 tokens: under the 5000 threshold on its own.
    await _seed_history(soul, 18000)
    # The fresh message (~600 tokens) pushes the window over the threshold.
    outcome = await soul._turn(Message(role="user", content="fresh prompt " + "z" * 2400))

    assert outcome.stop_reason == "no_tool_calls"
    assert provider.calls, "provider saw no requests at all"
    is_compaction, compaction_history_texts = provider.calls[0]
    assert is_compaction, (
        "the first request must be the compaction call; got a regular step first "
        f"(calls: {[c[0] for c in provider.calls]})"
    )
    assert all("fresh prompt" not in text for text in compaction_history_texts), (
        "the compaction request must NOT contain the new user message — "
        "it must run before the message is appended"
    )
    # After compacting, the new message is appended to the compacted history
    # and the turn runs a normal step.
    assert any(not c[0] for c in provider.calls[1:])


@pytest.mark.asyncio
async def test_tool_schema_tokens_count_toward_trigger(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """History well under the threshold, but history + schema is over it."""
    provider = _RecordingProvider()
    toolset = KimoToolset()
    toolset.add(_FatTool())
    llm = LLM(chat_provider=provider, max_context_size=10000, capabilities=set())
    soul = _make_soul(runtime, llm, toolset, tmp_path)
    soul._loop_control = _tight_loop_control()

    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    async def _noop_checkpoint() -> None:
        return None

    monkeypatch.setattr(soul, "_checkpoint", _noop_checkpoint)
    monkeypatch.setattr(soul._denwa_renji, "set_n_checkpoints", lambda _n: None)

    # ~12000 chars ≈ 3000 tokens of history + ~25 tokens of new message: the
    # message history alone (~3025) stays under 5000, but +~2000 schema tokens
    # crosses it. Without schema in the trigger, no compaction would fire.
    await _seed_history(soul, 12000)
    await soul._turn(Message(role="user", content="z" * 100))

    assert provider.calls and provider.calls[0][0], (
        "schema tokens must count toward the trigger: expected a compaction request first, got none"
    )


@pytest.mark.asyncio
async def test_no_compaction_when_everything_fits(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Small history + small schema: the first request is a normal step."""
    provider = _RecordingProvider()
    toolset = KimoToolset()
    toolset.add(_SmallTool())
    llm = LLM(chat_provider=provider, max_context_size=10000, capabilities=set())
    soul = _make_soul(runtime, llm, toolset, tmp_path)
    soul._loop_control = _tight_loop_control()

    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    async def _noop_checkpoint() -> None:
        return None

    monkeypatch.setattr(soul, "_checkpoint", _noop_checkpoint)
    monkeypatch.setattr(soul._denwa_renji, "set_n_checkpoints", lambda _n: None)

    await _seed_history(soul, 4000)  # ~1000 tokens; way under 5000 even with schema
    outcome = await soul._turn(Message(role="user", content="hi"))

    assert outcome.stop_reason == "no_tool_calls"
    assert provider.calls and not provider.calls[0][0], (
        "nothing should be compacted when the request comfortably fits"
    )
