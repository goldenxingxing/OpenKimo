"""A reply cut off by the output token limit does not end the turn.

Before, the loop could not tell a cut-off reply from a finished one: a reply with no
tool calls ended the turn, so the user got half an answer as the final result.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Self

import pytest
from kosong.chat_provider import FinishReason, StreamedMessagePart, ThinkingEffort, TokenUsage
from kosong.message import Message, TextPart
from kosong.tooling import Tool

import kimo_cli.soul.kimosoul as kimosoul_module
from kimo_cli.llm import LLM
from kimo_cli.soul.agent import Runtime
from kimo_cli.soul.kimosoul import MAX_TRUNCATION_CONTINUATIONS, KimoSoul
from kimo_cli.soul.toolset import KimoToolset
from tests.core.test_kimosoul_repeat import _make_soul


class _Stream:
    def __init__(self, text: str, finish_reason: FinishReason | None) -> None:
        self._text = text
        self._finish_reason = finish_reason
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

    @property
    def finish_reason(self) -> FinishReason | None:
        return self._finish_reason


class _ScriptedProvider:
    """Replies with the scripted (text, finish_reason) pairs in order."""

    name = "scripted"

    def __init__(self, script: list[tuple[str, FinishReason | None]]) -> None:
        self._script = script
        self.histories: list[list[Message]] = []

    @property
    def model_name(self) -> str:
        return "scripted"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self, system_prompt: str, tools: Sequence[Tool], history: Sequence[Message]
    ) -> _Stream:
        self.histories.append(list(history))
        index = min(len(self.histories) - 1, len(self._script) - 1)
        return _Stream(*self._script[index])

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


def _soul(
    runtime: Runtime, provider: _ScriptedProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> KimoSoul:
    llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
    soul = _make_soul(runtime, llm, KimoToolset(), tmp_path)
    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    async def _noop_checkpoint() -> None:
        return None

    monkeypatch.setattr(soul, "_checkpoint", _noop_checkpoint)
    monkeypatch.setattr(soul._denwa_renji, "set_n_checkpoints", lambda _n: None)
    return soul


def _texts(history: list[Message]) -> str:
    return "\n".join(part.text for m in history for part in m.content if isinstance(part, TextPart))


async def test_a_cut_off_reply_is_continued_instead_of_ending_the_turn(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ScriptedProvider([("The summary is: first, the con", "length"), ("fig.", "stop")])
    soul = _soul(runtime, provider, tmp_path, monkeypatch)

    outcome = await soul._turn(Message(role="user", content="summarise"))

    assert outcome.stop_reason == "no_tool_calls"
    assert len(provider.histories) == 2
    # the second request carries the cut-off reply and asks to continue it
    second = _texts(provider.histories[1])
    assert "The summary is: first, the con" in second
    assert "output token limit" in second
    assert outcome.final_message is not None
    assert "fig." in _texts([outcome.final_message])


async def test_continuations_are_capped(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ScriptedProvider([("more", "length")])
    soul = _soul(runtime, provider, tmp_path, monkeypatch)

    outcome = await soul._turn(Message(role="user", content="write a novel"))

    assert outcome.stop_reason == "no_tool_calls"
    assert len(provider.histories) == 1 + MAX_TRUNCATION_CONTINUATIONS


async def test_a_finished_reply_ends_the_turn_as_before(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for finish_reason in ("stop", None):
        provider = _ScriptedProvider([("done", finish_reason)])
        soul = _soul(runtime, provider, tmp_path / str(finish_reason), monkeypatch)
        (tmp_path / str(finish_reason)).mkdir(exist_ok=True)

        outcome = await soul._turn(Message(role="user", content="hi"))

        assert outcome.stop_reason == "no_tool_calls"
        assert len(provider.histories) == 1
