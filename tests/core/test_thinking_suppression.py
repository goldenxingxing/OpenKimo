"""Detection of server-side thinking suppression (KimoSoul._thinking_was_suppressed)."""

from kosong import StepResult
from kosong.chat_provider import TokenUsage
from kosong.message import Message, TextPart, ThinkPart

from kimo_cli.soul.kimosoul import KimoSoul


def _result(reasoning_output: int | None, content: list) -> StepResult:
    return StepResult(
        id="msg-1",
        message=Message(role="assistant", content=content),
        usage=(
            TokenUsage(input_other=10, output=5, reasoning_output=reasoning_output)
            if reasoning_output is not None
            else None
        ),
        tool_calls=[],
        _tool_result_futures={},
    )


def test_suppressed_when_marker_token_and_no_think_content():
    # Some reasoning endpoints mark suppression with reasoning_tokens<=1
    # and no reasoning content; the text output is still valid.
    result = _result(1, [TextPart(text="2")])
    assert KimoSoul._thinking_was_suppressed(result) is True


def test_not_suppressed_when_reasoning_present():
    result = _result(37, [ThinkPart(think="let me think"), TextPart(text="2")])
    assert KimoSoul._thinking_was_suppressed(result) is False


def test_not_suppressed_when_reasoning_tokens_normal():
    result = _result(120, [TextPart(text="2")])
    assert KimoSoul._thinking_was_suppressed(result) is False


def test_unknown_when_usage_missing():
    result = _result(None, [TextPart(text="2")])
    assert KimoSoul._thinking_was_suppressed(result) is False


def test_unknown_when_reasoning_output_not_reported():
    result = StepResult(
        id="msg-1",
        message=Message(role="assistant", content=[TextPart(text="2")]),
        usage=TokenUsage(input_other=10, output=5),
        tool_calls=[],
        _tool_result_futures={},
    )
    assert KimoSoul._thinking_was_suppressed(result) is False
