"""A model whose context window is unknown (max_context_size <= 0) is usable.

`llm.py` already treats a non-positive window as unknown, but compaction did not:
with a window of 0, `token_count >= 0 * ratio` is always true, so every step would
compact. And the context-usage ratio divided by the window.
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
from kimo_cli.soul.compaction import should_auto_compact
from kimo_cli.soul.toolset import KimoToolset
from tests.core.test_kimosoul_repeat import _make_soul


@pytest.mark.parametrize("window", [0, -1])
def test_an_unknown_window_never_triggers_token_based_compaction(window: int) -> None:
    assert not should_auto_compact(50_000, window, trigger_ratio=0.95, reserved_context_size=50_000)


def test_the_request_size_limit_still_applies_when_the_window_is_unknown() -> None:
    assert should_auto_compact(
        10,
        0,
        trigger_ratio=0.95,
        reserved_context_size=50_000,
        request_bytes=2_000_000,
        max_request_bytes=1_800_000,
    )


def test_known_windows_behave_as_before() -> None:
    assert should_auto_compact(96_000, 100_000, trigger_ratio=0.95, reserved_context_size=1_000)
    assert should_auto_compact(60_000, 100_000, trigger_ratio=0.95, reserved_context_size=50_000)
    assert not should_auto_compact(
        10_000, 100_000, trigger_ratio=0.95, reserved_context_size=50_000
    )


class _Stream:
    def __init__(self) -> None:
        self._iter = self._parts()

    async def _parts(self) -> AsyncIterator[StreamedMessagePart]:
        yield TextPart(text="hello")

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        return await self._iter.__anext__()

    @property
    def id(self) -> str | None:
        return None

    @property
    def usage(self) -> TokenUsage | None:
        return TokenUsage(input_other=120_000, output=10)


class _Provider:
    name = "unknown-window"

    def __init__(self) -> None:
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "unknown-window"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self, system_prompt: str, tools: Sequence[Tool], history: Sequence[Message]
    ) -> _Stream:
        self.calls += 1
        return _Stream()

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


async def test_a_turn_runs_without_compacting_or_crashing(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _Provider()
    llm = LLM(chat_provider=provider, max_context_size=0, capabilities=set())
    soul = _make_soul(runtime, llm, KimoToolset(), tmp_path)
    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    async def _noop_checkpoint() -> None:
        return None

    compactions: list[bool] = []

    async def _record_compaction(**_: object) -> None:
        compactions.append(True)

    monkeypatch.setattr(soul, "_checkpoint", _noop_checkpoint)
    monkeypatch.setattr(soul._denwa_renji, "set_n_checkpoints", lambda _n: None)
    monkeypatch.setattr(soul, "compact_context", _record_compaction)

    for _ in range(2):  # the second turn starts with a large reported token count
        outcome = await soul._turn(Message(role="user", content="hi"))
        assert outcome.stop_reason == "no_tool_calls"

    assert compactions == []
    assert soul.status.context_usage == 0.0
