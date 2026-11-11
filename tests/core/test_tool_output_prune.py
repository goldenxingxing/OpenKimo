"""Old tool outputs are cleared from requests in batches that keep the prefix stable."""

from __future__ import annotations

import json
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
from kimo_cli.soul.tool_output_prune import ToolOutputPruner
from kimo_cli.soul.toolset import KimoToolset
from tests.core.test_kimosoul_repeat import _make_soul

OUTPUT_CHARS = 8_000  # ~2k tokens per tool output


def _tool_round(i: int) -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[TextPart(text=f"step {i}")],
            tool_calls=[
                ToolCall(id=f"c{i}", function=ToolCall.FunctionBody(name="Grep", arguments="{}"))
            ],
        ),
        Message(
            role="tool", tool_call_id=f"c{i}", content=[TextPart(text=f"{i}:" + "o" * OUTPUT_CHARS)]
        ),
    ]


def _history(rounds: int) -> list[Message]:
    history = [Message(role="user", content=[TextPart(text="go")])]
    for i in range(rounds):
        history.extend(_tool_round(i))
    return history


def test_nothing_is_cleared_while_old_output_is_under_the_minimum() -> None:
    pruner = ToolOutputPruner(protect_tokens=40_000, minimum_tokens=20_000)
    # 20 outputs fill the protected 40k; 9 more (~18k) are not yet worth clearing
    assert not pruner.update(_history(29))
    assert pruner.view(_history(29)) == _history(29)


def test_old_outputs_are_cleared_once_they_reach_the_minimum() -> None:
    pruner = ToolOutputPruner(protect_tokens=40_000, minimum_tokens=20_000)
    history = _history(31)

    assert pruner.update(history)
    view = pruner.view(history)

    cleared = [
        m for m in view if m.role == "tool" and "cleared to save context" in m.extract_text()
    ]
    kept = [m for m in view if m.role == "tool" and "cleared" not in m.extract_text()]
    # 20 outputs (~2k tokens each) fill the protected 40k; the 11 before them go
    assert len(cleared) == 11
    assert len(kept) == 20
    # the oldest go first, and every tool call keeps a result
    assert cleared[0].tool_call_id == "c0"
    assert "Grep" in cleared[0].extract_text()
    assert [m.tool_call_id for m in view if m.role == "tool"] == [f"c{i}" for i in range(31)]


def test_the_request_prefix_only_changes_when_a_batch_is_cleared() -> None:
    pruner = ToolOutputPruner(protect_tokens=40_000, minimum_tokens=20_000)
    previous: list[Message] | None = None
    batches = 0
    prefix_kept = 0
    for rounds in range(1, 80):
        history = _history(rounds)
        batched = pruner.update(history)
        view = pruner.view(history)
        if previous is not None:
            if batched:
                batches += 1
            else:
                assert view[: len(previous)] == previous  # cache-stable
                prefix_kept += 1
        previous = view

    # ~2k tokens per step and 20k per batch: one batch every ~10 steps
    assert 4 <= batches <= 7
    assert prefix_kept >= 70


def test_the_history_itself_is_not_changed() -> None:
    pruner = ToolOutputPruner(protect_tokens=40_000, minimum_tokens=20_000)
    history = _history(40)
    original = [m.model_copy(deep=True) for m in history]

    pruner.update(history)
    pruner.view(history)

    assert history == original


# --- in the agent loop ---------------------------------------------------------------


class _Params(BaseModel):
    pass


class _Grep(CallableTool2[_Params]):
    name: str = "Grep"
    description: str = "search"
    params: type[_Params] = _Params
    calls: int = 0

    async def __call__(self, params: _Params) -> ToolReturnValue:
        type(self).calls += 1
        return ToolOk(output=f"{type(self).calls}:" + "o" * OUTPUT_CHARS)


class _Stream:
    def __init__(self, parts: list[StreamedMessagePart]) -> None:
        self._iter = self._gen(parts)

    async def _gen(self, parts: list[StreamedMessagePart]) -> AsyncIterator[StreamedMessagePart]:
        for part in parts:
            yield part

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


class _SearchesThenAnswers:
    name = "searcher"

    def __init__(self, searches: int) -> None:
        self.searches = searches
        self.requests: list[list[Message]] = []

    @property
    def model_name(self) -> str:
        return "searcher"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self, system_prompt: str, tools: Sequence[Tool], history: Sequence[Message]
    ) -> _Stream:
        self.requests.append(list(history))
        n = len(self.requests)
        if n > self.searches:
            return _Stream([TextPart(text="done")])
        return _Stream(
            [
                TextPart(text=f"search {n}"),
                ToolCall(
                    id=f"s{n}",
                    function=ToolCall.FunctionBody(name="Grep", arguments=json.dumps({"q": n})),
                ),
            ]
        )

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


async def test_long_sessions_send_cleared_outputs_and_keep_the_full_history(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _Grep.calls = 0
    toolset = KimoToolset()
    toolset.add(_Grep())
    provider = _SearchesThenAnswers(searches=40)
    llm = LLM(chat_provider=provider, max_context_size=1_000_000, capabilities=set())
    soul = _make_soul(runtime, llm, toolset, tmp_path)
    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    async def _noop_checkpoint() -> None:
        return None

    monkeypatch.setattr(soul, "_checkpoint", _noop_checkpoint)
    monkeypatch.setattr(soul._denwa_renji, "set_n_checkpoints", lambda _n: None)

    await soul._turn(Message(role="user", content="search a lot"))

    last_request = provider.requests[-1]
    cleared = [
        m
        for m in last_request
        if m.role == "tool" and "cleared to save context" in m.extract_text()
    ]
    assert len(cleared) >= 10
    # the history on disk still has every output in full
    full = [m for m in soul._context.history if m.role == "tool"]
    assert len(full) == 40
    assert all("cleared" not in m.extract_text() for m in full)
