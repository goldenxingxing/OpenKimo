"""A response cut off by the output token limit has to be visible to the caller.

Before, no provider read the stop reason, so a reply cut off mid-sentence looked
exactly like a finished one, and a tool call cut off mid-arguments was executed.
"""

import asyncio
from typing import override

import pytest

from kosong import generate, step
from kosong.chat_provider import StreamedMessagePart, normalize_finish_reason
from kosong.chat_provider.mock import MockChatProvider
from kosong.message import ToolCall
from kosong.tooling import CallableTool, ParametersType, ToolOk, ToolReturnValue
from kosong.tooling.simple import SimpleToolset


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("stop", "stop"),
        ("length", "length"),
        ("tool_calls", "tool_calls"),
        ("content_filter", "content_filter"),
        # Anthropic
        ("end_turn", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_calls"),
        ("refusal", "content_filter"),
        # Google
        ("STOP", "stop"),
        ("MAX_TOKENS", "length"),
        ("SAFETY", "content_filter"),
        # OpenAI Responses
        ("max_output_tokens", "length"),
        ("something_new", "other"),
        (None, None),
        ("", None),
    ],
)
def test_normalize_finish_reason(raw: str | None, expected: str | None) -> None:
    assert normalize_finish_reason(raw) == expected


class _PlusTool(CallableTool):
    name: str = "plus"
    description: str = "Add two integers."
    parameters: ParametersType = {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
    }
    calls: int = 0

    @override
    async def __call__(self, a: int, b: int) -> ToolReturnValue:
        type(self).calls += 1
        return ToolOk(output=str(a + b))


def _truncated_parts() -> list[StreamedMessagePart]:
    return [
        ToolCall(
            id="complete",
            function=ToolCall.FunctionBody(name="plus", arguments='{"a": 1, "b": 2}'),
        ),
        ToolCall(
            id="cut",
            function=ToolCall.FunctionBody(name="plus", arguments='{"a": 1, "b'),
        ),
    ]


async def test_generate_does_not_dispatch_a_tool_call_cut_off_by_the_length_limit() -> None:
    provider = MockChatProvider(message_parts=_truncated_parts(), finish_reason="length")
    dispatched: list[str] = []

    async def on_tool_call(tool_call: ToolCall) -> None:
        dispatched.append(tool_call.id)

    result = await generate(provider, "", [], [], on_tool_call=on_tool_call)

    assert result.finish_reason == "length"
    assert dispatched == ["complete"]
    assert [tc.id for tc in result.message.tool_calls or []] == ["complete", "cut"]
    assert [tc.id for tc in result.undispatched_tool_calls] == ["cut"]


async def test_generate_dispatches_the_last_tool_call_when_the_reply_finished() -> None:
    provider = MockChatProvider(message_parts=_truncated_parts(), finish_reason="tool_calls")
    dispatched: list[str] = []

    async def on_tool_call(tool_call: ToolCall) -> None:
        dispatched.append(tool_call.id)

    result = await generate(provider, "", [], [], on_tool_call=on_tool_call)

    assert dispatched == ["complete", "cut"]
    assert result.undispatched_tool_calls == []


async def test_step_answers_a_cut_off_tool_call_with_an_error_instead_of_running_it() -> None:
    _PlusTool.calls = 0
    provider = MockChatProvider(message_parts=_truncated_parts(), finish_reason="length")
    result = await step(provider, "", SimpleToolset([_PlusTool()]), [])

    assert result.finish_reason == "length"
    tool_results = await result.tool_results()
    assert [r.tool_call_id for r in tool_results] == ["complete", "cut"]
    assert tool_results[0].return_value.is_error is False
    assert tool_results[1].return_value.is_error is True
    assert "output token limit" in tool_results[1].return_value.message
    # only the complete call ran
    assert _PlusTool.calls == 1


def test_step_result_tool_results_have_one_entry_per_tool_call() -> None:
    """History needs a result for every tool call, including the one not run."""
    _PlusTool.calls = 0

    async def run() -> None:
        provider = MockChatProvider(message_parts=_truncated_parts(), finish_reason="length")
        result = await step(provider, "", SimpleToolset([_PlusTool()]), [])
        assert len(await result.tool_results()) == len(result.tool_calls) == 2

    asyncio.run(run())
