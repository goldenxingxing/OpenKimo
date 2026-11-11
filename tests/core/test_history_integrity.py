"""Every tool call the model sees has its result right after it.

A worker killed between writing a step's assistant message and its tool results left
a tool call with no result on disk, and a provider rejects that history with a 400
on the next request. Two defences: a step is written in one write, and the history
sent to the provider is repaired — a view, the file is not rewritten.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Self

import pytest
from kosong.chat_provider import StreamedMessagePart, ThinkingEffort, TokenUsage
from kosong.message import Message, TextPart, ToolCall
from kosong.tooling import Tool

import kimo_cli.soul.kimosoul as kimosoul_module
from kimo_cli.llm import LLM
from kimo_cli.soul.agent import Runtime
from kimo_cli.soul.context import Context
from kimo_cli.soul.history_repair import MISSING_TOOL_RESULT_TEXT, repair_tool_pairing
from kimo_cli.soul.toolset import KimoToolset
from tests.core.test_kimosoul_repeat import _make_soul


def _call(*ids: str) -> Message:
    return Message(
        role="assistant",
        content=[TextPart(text="calling")],
        tool_calls=[
            ToolCall(id=i, function=ToolCall.FunctionBody(name="Grep", arguments="{}")) for i in ids
        ],
    )


def _result(call_id: str, text: str = "ok") -> Message:
    return Message(role="tool", tool_call_id=call_id, content=[TextPart(text=text)])


def _user(text: str) -> Message:
    return Message(role="user", content=[TextPart(text=text)])


def _shape(history: list[Message]) -> list[str]:
    return [f"{m.role}:{m.tool_call_id}" if m.role == "tool" else f"{m.role}" for m in history]


def test_a_well_formed_history_is_returned_unchanged() -> None:
    history = [_user("go"), _call("a", "b"), _result("a"), _result("b"), _user("next")]

    assert repair_tool_pairing(history) == history


def test_a_missing_result_gets_a_synthetic_one() -> None:
    history = [_user("go"), _call("a", "b"), _result("a"), _user("next")]

    repaired = repair_tool_pairing(history)

    assert _shape(repaired) == ["user", "assistant", "tool:a", "tool:b", "user"]
    assert repaired[3].extract_text() == MISSING_TOOL_RESULT_TEXT


def test_a_result_with_no_call_is_dropped() -> None:
    history = [_user("go"), _result("ghost"), _call("a"), _result("a")]

    assert _shape(repair_tool_pairing(history)) == ["user", "assistant", "tool:a"]


def test_a_displaced_result_is_moved_back_after_its_call() -> None:
    history = [_user("go"), _call("a"), _user("steer"), _result("a", "real")]

    repaired = repair_tool_pairing(history)

    assert _shape(repaired) == ["user", "assistant", "tool:a", "user"]
    assert repaired[2].extract_text() == "real"


def test_a_duplicate_result_is_dropped() -> None:
    history = [_call("a"), _result("a", "first"), _result("a", "second")]

    repaired = repair_tool_pairing(history)

    assert _shape(repaired) == ["assistant", "tool:a"]
    assert repaired[1].extract_text() == "first"


def test_an_id_reused_by_a_later_call_is_matched_in_order() -> None:
    history = [_call("x"), _result("x", "one"), _user("u"), _call("x"), _result("x", "two")]

    repaired = repair_tool_pairing(history)

    assert _shape(repaired) == ["assistant", "tool:x", "user", "assistant", "tool:x"]
    assert [m.extract_text() for m in repaired if m.role == "tool"] == ["one", "two"]


# --- the loop --------------------------------------------------------------------


class _Stream:
    def __init__(self) -> None:
        self._iter = self._gen()

    async def _gen(self) -> AsyncIterator[StreamedMessagePart]:
        yield TextPart(text="answer")

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        return await self._iter.__anext__()

    @property
    def id(self) -> str | None:
        return None

    @property
    def usage(self) -> TokenUsage | None:
        return TokenUsage(input_other=100, output=5)


class _Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.requests: list[list[Message]] = []

    @property
    def model_name(self) -> str:
        return "recorder"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self, system_prompt: str, tools: Sequence[Tool], history: Sequence[Message]
    ) -> _Stream:
        self.requests.append(list(history))
        return _Stream()

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


async def test_a_session_killed_mid_step_can_carry_on(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # what a kill between the two writes of the old code left on disk
    file = tmp_path / "history.jsonl"
    with file.open("w") as f:
        for message in (_user("find the bug"), _call("lost")):
            f.write(message.model_dump_json(exclude_none=True) + "\n")
    before = file.read_text()

    provider = _Recorder()
    llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
    soul = _make_soul(runtime, llm, KimoToolset(), tmp_path)
    await soul._context.restore()
    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    async def _noop_checkpoint() -> None:
        return None

    monkeypatch.setattr(soul, "_checkpoint", _noop_checkpoint)
    monkeypatch.setattr(soul._denwa_renji, "set_n_checkpoints", lambda _n: None)

    await soul._turn(Message(role="user", content="continue"))

    sent = provider.requests[0]
    lost_call = next(i for i, m in enumerate(sent) if m.tool_calls)
    assert sent[lost_call + 1].role == "tool"
    assert sent[lost_call + 1].tool_call_id == "lost"
    # the file was not rewritten: it only grew
    assert file.read_text().startswith(before)


async def test_a_step_is_recorded_in_one_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiofiles

    file = tmp_path / "history.jsonl"
    context = Context(file_backend=file)
    writes: list[str] = []
    real_open = aiofiles.open

    class _File:
        def __init__(self, f) -> None:  # noqa: ANN001
            self._f = f

        async def write(self, data: str) -> int:
            writes.append(data)
            return await self._f.write(data)

    class _Open:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            self._cm = real_open(*args, **kwargs)

        async def __aenter__(self) -> _File:
            return _File(await self._cm.__aenter__())

        async def __aexit__(self, *exc) -> None:  # noqa: ANN002
            await self._cm.__aexit__(*exc)

    monkeypatch.setattr("kimo_cli.soul.context.aiofiles.open", _Open)

    await context.append_step(_call("a", "b"), [_result("a"), _result("b")], token_count=123)

    assert len(writes) == 1
    monkeypatch.undo()
    restored = Context(file_backend=file)
    await restored.restore()
    assert _shape(list(restored.history)) == ["assistant", "tool:a", "tool:b"]
    assert restored.token_count == context.token_count == 123
