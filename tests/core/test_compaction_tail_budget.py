"""Compaction keeps as much recent history as a token budget allows.

It used to keep exactly the last two user/assistant messages, whatever their size:
after compaction the agent often lost the steps it had just taken, or kept one huge
tool output and saved almost nothing. Now it keeps the recent messages that fit in
`preserved_tail_budget(window)` — opencode's clamp(2k, 15k, 25% of the window) — and
cuts only where no tool call is separated from its result.
"""

from __future__ import annotations

import pytest
from kosong.message import Message, TextPart, ToolCall

from kimo_cli.soul.compaction import (
    SimpleCompaction,
    estimate_message_bytes,
    preserved_tail_budget,
)


def _tokens(messages: list[Message]) -> int:
    return estimate_message_bytes(messages) // 4


def _conversation(exchanges: int, chars: int) -> list[Message]:
    messages: list[Message] = []
    for i in range(exchanges):
        messages.append(Message(role="user", content=[TextPart(text=f"q{i} " + "x" * chars)]))
        messages.append(
            Message(
                role="assistant",
                content=[TextPart(text=f"a{i}")],
                tool_calls=[
                    ToolCall(
                        id=f"call{i}", function=ToolCall.FunctionBody(name="Grep", arguments="{}")
                    )
                ],
            )
        )
        messages.append(
            Message(role="tool", tool_call_id=f"call{i}", content=[TextPart(text="y" * chars)])
        )
        messages.append(Message(role="assistant", content=[TextPart(text=f"done {i}")]))
    return messages


@pytest.mark.parametrize(
    ("window", "budget"),
    [(1_000_000, 15_000), (200_000, 15_000), (40_000, 10_000), (8_000, 2_000), (0, 15_000)],
)
def test_budget_is_a_quarter_of_the_window_between_2k_and_15k(window: int, budget: int) -> None:
    assert preserved_tail_budget(window) == budget


def test_keeps_the_recent_messages_that_fit_in_the_budget() -> None:
    messages = _conversation(exchanges=40, chars=2_000)  # ~1k tokens per exchange

    prepared = SimpleCompaction().prepare(messages, max_context_size=200_000)
    preserved = list(prepared.to_preserve)

    assert prepared.compact_message is not None
    assert _tokens(preserved) <= 15_000
    # far more than the old two messages, and close to the budget
    assert len(preserved) > 20
    assert _tokens(preserved) > 12_000


def test_never_separates_a_tool_call_from_its_result() -> None:
    messages = _conversation(exchanges=40, chars=2_000)

    for window in (8_000, 40_000, 200_000):
        preserved = list(SimpleCompaction().prepare(messages, max_context_size=window).to_preserve)
        assert preserved[0].role in {"user", "assistant"}
        call_ids = {tc.id for m in preserved for tc in (m.tool_calls or [])}
        result_ids = {m.tool_call_id for m in preserved if m.role == "tool"}
        assert result_ids <= call_ids


def test_keeps_the_newest_group_even_when_it_alone_exceeds_the_budget() -> None:
    messages = _conversation(exchanges=3, chars=200)
    messages.append(Message(role="user", content=[TextPart(text="huge " + "z" * 200_000)]))

    prepared = SimpleCompaction().prepare(messages, max_context_size=200_000)

    assert prepared.compact_message is not None
    assert list(prepared.to_preserve) == messages[-1:]


def test_a_short_history_is_still_compacted_as_before() -> None:
    messages = _conversation(exchanges=3, chars=50)  # well under the budget

    with_budget = SimpleCompaction().prepare(messages, max_context_size=200_000)
    old = SimpleCompaction().prepare(messages)

    assert with_budget.compact_message is not None
    assert list(with_budget.to_preserve) == list(old.to_preserve)


def test_without_a_window_the_old_rule_applies() -> None:
    messages = _conversation(exchanges=40, chars=2_000)

    prepared = SimpleCompaction(max_preserved_messages=2).prepare(messages)

    # the last two user/assistant messages: "a39" with its tool result, and "done 39"
    assert list(prepared.to_preserve) == messages[-3:]
