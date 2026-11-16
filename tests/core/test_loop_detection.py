"""A loop of calls that keep getting the same results ends the turn.

The existing check only counts the same call made in consecutive steps. An agent
going round in a circle — A, B, A, B, or the same failing command between reads of
other files — resets that count every step and runs until the step limit.

Like crush, a call is identified with its result, so a call that keeps returning
something new (progress) is never a loop.
"""

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
from kimo_cli.soul.toolset import KimoToolset
from tests.core.test_kimosoul_repeat import _make_soul


class _Params(BaseModel):
    q: str = ""


def _tool(name: str, output) -> CallableTool2[_Params]:  # noqa: ANN001
    async def __call__(self: CallableTool2[_Params], params: _Params) -> ToolReturnValue:
        return ToolOk(output=output(params.q))

    cls = type(
        f"_T_{name}",
        (CallableTool2,),
        {"name": name, "description": name, "params": _Params, "__call__": __call__},
    )
    return cls()


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


class _Script:
    """Makes the scripted (tool, q) call each step, then answers."""

    name = "script"

    def __init__(self, calls: list[tuple[str, str]]) -> None:
        self.calls = calls
        self.requests = 0

    @property
    def model_name(self) -> str:
        return "script"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self, system_prompt: str, tools: Sequence[Tool], history: Sequence[Message]
    ) -> _Stream:
        self.requests += 1
        if self.requests > len(self.calls):
            return _Stream([TextPart(text="done")])
        name, q = self.calls[self.requests - 1]
        return _Stream(
            [
                ToolCall(
                    id=f"c{self.requests}",
                    function=ToolCall.FunctionBody(name=name, arguments=json.dumps({"q": q})),
                )
            ]
        )

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


async def _turn(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: list[CallableTool2[_Params]],
    calls: list[tuple[str, str]],
):  # noqa: ANN202
    toolset = KimoToolset()
    for tool in tools:
        toolset.add(tool)
    provider = _Script(calls)
    llm = LLM(chat_provider=provider, max_context_size=1_000_000, capabilities=set())
    soul = _make_soul(runtime, llm, toolset, tmp_path)
    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    async def _noop_checkpoint() -> None:
        return None

    monkeypatch.setattr(soul, "_checkpoint", _noop_checkpoint)
    monkeypatch.setattr(soul._denwa_renji, "set_n_checkpoints", lambda _n: None)
    outcome = await soul._turn(Message(role="user", content="go"))
    return outcome, provider


def _same(_q: str) -> str:
    return "same result"


async def test_a_two_step_circle_ends_the_turn(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = [("Grep", "x"), ("Glob", "y")] * 20
    outcome, provider = await _turn(
        runtime, tmp_path, monkeypatch, [_tool("Grep", _same), _tool("Glob", _same)], calls
    )

    assert outcome.stop_reason == "tool_call_repeat"
    assert provider.requests <= 12


async def test_the_same_call_between_other_calls_ends_the_turn(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    for i in range(20):
        calls += [("Shell", "make test"), ("ReadFile", f"file{i}.py")]
    outcome, provider = await _turn(
        runtime,
        tmp_path,
        monkeypatch,
        [_tool("Shell", lambda q: "1 failed"), _tool("ReadFile", lambda q: f"contents of {q}")],
        calls,
    )

    assert outcome.stop_reason == "tool_call_repeat"
    assert provider.requests <= 12


async def test_calls_that_keep_returning_something_new_are_not_a_loop(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = iter(range(1000))
    calls = [("Grep", "x"), ("Glob", "y")] * 10
    outcome, provider = await _turn(
        runtime,
        tmp_path,
        monkeypatch,
        [
            _tool("Grep", lambda q: f"new {next(counter)}"),
            _tool("Glob", lambda q: f"new {next(counter)}"),
        ],
        calls,
    )

    assert outcome.stop_reason == "no_tool_calls"
    assert provider.requests == len(calls) + 1


async def test_polling_a_background_task_is_not_a_loop(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # polling one task while doing different work in between
    calls = [c for i in range(10) for c in (("TaskOutput", "t1"), ("Glob", f"dir{i}"))]
    outcome, provider = await _turn(
        runtime,
        tmp_path,
        monkeypatch,
        [_tool("TaskOutput", lambda q: "still running"), _tool("Glob", lambda q: f"files {q}")],
        calls,
    )

    assert outcome.stop_reason == "no_tool_calls"
    assert provider.requests == len(calls) + 1
