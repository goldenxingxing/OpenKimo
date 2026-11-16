"""A failed compaction does not end the turn.

Compaction is upkeep. When the one run before a step failed, the error went to the
fatal branch and the turn ended, although the request might still have fit. Now the
step runs anyway; if the request really is too large, the provider says so and the
existing overflow handling takes over.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Self

import pytest
from kosong.chat_provider import StreamedMessagePart, ThinkingEffort, TokenUsage
from kosong.message import Message, TextPart
from kosong.tooling import Tool

import kimo_cli.soul.kimosoul as kimosoul_module
from kimo_cli.llm import LLM
from kimo_cli.soul.agent import Runtime
from kimo_cli.soul.compaction import COMPACTION_SYSTEM_PROMPT
from kimo_cli.soul.toolset import KimoToolset
from kimo_cli.wire.types import CompactionBegin, CompactionEnd
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


class _CompactionFails:
    """Answers normal requests; fails every compaction request."""

    name = "compaction-fails"

    def __init__(self) -> None:
        self.compaction_requests = 0
        self.step_requests = 0

    @property
    def model_name(self) -> str:
        return "compaction-fails"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self, system_prompt: str, tools: Sequence[Tool], history: Sequence[Message]
    ) -> _Stream:
        if system_prompt == COMPACTION_SYSTEM_PROMPT:
            self.compaction_requests += 1
            raise ValueError("compaction model unavailable")
        self.step_requests += 1
        return _Stream("answer")

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


async def test_the_step_runs_when_compaction_fails(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _CompactionFails()
    llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
    soul = _make_soul(runtime, llm, KimoToolset(), tmp_path)
    sent: list[object] = []
    monkeypatch.setattr(kimosoul_module, "wire_send", sent.append)
    monkeypatch.setattr(kimosoul_module, "should_auto_compact", lambda *a, **k: True)

    async def _noop_checkpoint() -> None:
        return None

    monkeypatch.setattr(soul, "_checkpoint", _noop_checkpoint)
    monkeypatch.setattr(soul._denwa_renji, "set_n_checkpoints", lambda _n: None)
    for text in ("first question", "first answer", "second question", "second answer"):
        role = "user" if "question" in text else "assistant"
        await soul._context.append_message(Message(role=role, content=[TextPart(text=text)]))

    outcome = await soul._turn(Message(role="user", content="third question"))

    assert provider.compaction_requests >= 1
    assert provider.step_requests == 1
    assert outcome.stop_reason == "no_tool_calls"
    # the history was not lost
    texts = [p.text for m in soul._context.history for p in m.content if isinstance(p, TextPart)]
    assert "first question" in texts


async def test_a_failed_compaction_still_closes_its_begin_event(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _CompactionFails()
    llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
    soul = _make_soul(runtime, llm, KimoToolset(), tmp_path)
    sent: list[object] = []
    monkeypatch.setattr(kimosoul_module, "wire_send", sent.append)
    for text in ("q1", "a1", "q2", "a2"):
        role = "user" if text.startswith("q") else "assistant"
        await soul._context.append_message(Message(role=role, content=[TextPart(text=text)]))

    with pytest.raises(ValueError):
        await soul.compact_context(manual=True)

    kinds = [type(event) for event in sent if isinstance(event, CompactionBegin | CompactionEnd)]
    assert kinds == [CompactionBegin, CompactionEnd]
