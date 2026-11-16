"""Tests for TodoSyncInjectionProvider.get_injections() flow."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

from kosong.message import Message, TextPart

from kimo_cli.session_state import TodoItemState
from kimo_cli.soul.dynamic_injections.todo_sync import (
    TodoSyncInjectionProvider,
    _full_reminder,
)


def _todos(*statuses: str) -> list[TodoItemState]:
    titles = ["item A", "item B", "item C", "item D", "item E", "item F", "item G"]
    return [TodoItemState(title=titles[i], status=s) for i, s in enumerate(statuses)]


def _make_soul_mock(todos: list[TodoItemState] | None, is_subagent: bool = False) -> MagicMock:
    soul = MagicMock()
    type(soul).is_subagent = PropertyMock(return_value=is_subagent)
    type(soul).todos = PropertyMock(return_value=todos or [])
    return soul


def _reminder_msg(content: str | None = None) -> Message:
    """Create a user message that looks like a todo sync reminder."""
    return Message(
        role="user",
        content=[TextPart(text=content or _full_reminder(_todos("done", "in_progress")))],
    )


def _assistant_msg() -> Message:
    return Message(role="assistant", content=[TextPart(text="step")])


class TestTodoSyncInjectionProvider:
    async def test_returns_empty_when_no_todos(self) -> None:
        provider = TodoSyncInjectionProvider()
        soul = _make_soul_mock([])

        result = await provider.get_injections([], soul)

        assert result == []

    async def test_returns_empty_when_all_done(self) -> None:
        provider = TodoSyncInjectionProvider()
        soul = _make_soul_mock(_todos("done", "done", "done"))

        result = await provider.get_injections([], soul)

        assert result == []

    async def test_skips_subagent(self) -> None:
        provider = TodoSyncInjectionProvider()
        soul = _make_soul_mock(_todos("done", "in_progress"), is_subagent=True)

        result = await provider.get_injections([], soul)

        assert result == []

    async def test_first_call_injects_full_reminder_with_list(self) -> None:
        provider = TodoSyncInjectionProvider()
        todos = _todos("done", "done", "done", "done", "in_progress", "pending", "pending")
        soul = _make_soul_mock(todos)

        result = await provider.get_injections([], soul)

        assert len(result) == 1
        assert result[0].type == "todo_sync"
        assert "Todo sync check." in result[0].content
        # The persisted list must be embedded so the model can produce a
        # correct sync call even after a compaction wiped its memory of it.
        assert "- [done] item A" in result[0].content
        assert "- [in_progress] item E" in result[0].content
        assert "- [pending] item G" in result[0].content

    async def test_throttled_before_interval(self) -> None:
        provider = TodoSyncInjectionProvider()
        soul = _make_soul_mock(_todos("done", "in_progress"))

        # History: reminder + 3 assistant steps (< 4 threshold)
        history = [_reminder_msg()] + [_assistant_msg() for _ in range(3)]

        result = await provider.get_injections(history, soul)

        assert result == []

    async def test_injects_sparse_after_interval_reached(self) -> None:
        provider = TodoSyncInjectionProvider()
        soul = _make_soul_mock(_todos("done", "in_progress"))

        # History: reminder + 4 assistant steps (= threshold)
        history = [_reminder_msg()] + [_assistant_msg() for _ in range(4)]

        result = await provider.get_injections(history, soul)

        assert len(result) == 1
        assert result[0].content.startswith("Todo sync:")
        assert "in_progress" in result[0].content

    async def test_full_reminder_after_compaction_drops_history(self) -> None:
        """After compaction the rebuilt history has no reminders, so the next
        injection must be the full version (list embedded)."""
        provider = TodoSyncInjectionProvider()
        todos = _todos("in_progress", "pending")
        soul = _make_soul_mock(todos)

        # Compacted history: just the summary-ish user message, no reminders.
        history = [Message(role="user", content=[TextPart(text="summary of prior work")])]

        result = await provider.get_injections(history, soul)

        assert len(result) == 1
        assert "Todo sync check." in result[0].content
        assert "- [in_progress] item A" in result[0].content

    async def test_detection_survives_history_normalization_merge(self) -> None:
        """normalize_history merges injection user messages into the adjacent
        user message; the reminder text still lives in a TextPart there."""
        provider = TodoSyncInjectionProvider()
        soul = _make_soul_mock(_todos("done", "in_progress"))

        merged = Message(
            role="user",
            content=[
                TextPart(text="please continue"),
                TextPart(
                    text=f"<system-reminder>\n{_full_reminder(_todos('done', 'in_progress'))}\n</system-reminder>"
                ),
            ],
        )
        history = [merged] + [_assistant_msg() for _ in range(4)]

        result = await provider.get_injections(history, soul)

        assert len(result) == 1
        assert result[0].content.startswith("Todo sync:")

    async def test_scan_window_bounds_cost(self) -> None:
        """A reminder older than the scan window is treated as absent; the
        provider still injects (full version) instead of silently never
        reminding on very long histories."""
        provider = TodoSyncInjectionProvider()
        todos = _todos("in_progress", "pending")
        soul = _make_soul_mock(todos)

        history = [_reminder_msg()] + [_assistant_msg() for _ in range(300)]

        result = await provider.get_injections(history, soul)

        assert len(result) == 1
        assert "Todo sync check." in result[0].content
