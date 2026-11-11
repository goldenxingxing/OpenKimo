"""Keeps the model's todo list in sync with what it has actually done.

## Why this exists

The Tasks panel the user sees is driven by the persisted todo list
(`state.json`), which only changes when the model calls ``SetTodoList``.
The failure mode: the model does the work across a long stretch of steps
— especially after a context compaction, where the tool description and
recent reminder context collapse into a summary — and ends the turn with
the list stale (e.g. panel shows 4/7 done while everything is finished).
Nothing in the runtime ever re-grounded the model against the *persisted*
list, so the drift could survive whole turns.

## Mechanism

A dynamic injection provider (same pattern as plan mode): before each LLM
step, if the persisted list has open items, inject the current list with
its statuses plus an explicit end-of-turn sync rule. Throttling is derived
from history — count assistant steps since the last todo reminder — so a
compaction (which drops prior reminders) automatically makes the next
reminder the full version.

Root session only: subagent todos live in their own state files, which
this provider cannot read, and the user-facing panel shows root todos.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from kosong.message import Message, TextPart

from kimo_cli.session_state import TodoItemState
from kimo_cli.soul.dynamic_injection import DynamicInjection, DynamicInjectionProvider

if TYPE_CHECKING:
    from kimo_cli.soul.kimosoul import KimoSoul

# Inject a reminder at most once every N assistant steps.
_TURN_INTERVAL = 4

# Backward history scan is bounded — a reminder older than this window is
# treated as absent, which only means the next reminder is the full version.
_SCAN_WINDOW = 200

_FULL_KEY = "Todo sync check."
_SPARSE_KEY = "Todo sync:"


class TodoSyncInjectionProvider(DynamicInjectionProvider):
    """Periodically injects the persisted todo list while it has open items."""

    async def get_injections(
        self,
        history: Sequence[Message],
        soul: KimoSoul,
    ) -> list[DynamicInjection]:
        # Subagent todos live in the subagent's own state file and are not
        # what the user's Tasks panel shows.
        if soul.is_subagent:
            return []

        todos = soul.todos
        if not todos or all(t.status == "done" for t in todos):
            return []

        turns_since_last = _steps_since_last_todo_reminder(history)
        if turns_since_last is not None and turns_since_last < _TURN_INTERVAL:
            return []

        content = _full_reminder(todos) if turns_since_last is None else _sparse_reminder(todos)
        return [DynamicInjection(type="todo_sync", content=content)]


def _steps_since_last_todo_reminder(history: Sequence[Message]) -> int | None:
    """Assistant steps since the last todo reminder, or None if none found."""
    assistant_steps = 0
    for msg in reversed(history[-_SCAN_WINDOW:]):
        if msg.role == "user" and _has_todo_reminder(msg):
            return assistant_steps
        if msg.role == "assistant":
            assistant_steps += 1
    return None


def _has_todo_reminder(msg: Message) -> bool:
    """Detect a todo sync reminder by matching stable prefixes of the actual
    reminder texts, so wording changes stay automatically in sync."""
    for part in msg.content:
        if isinstance(part, TextPart) and (_FULL_KEY in part.text or _SPARSE_KEY in part.text):
            return True
    return False


def _format_list(todos: list[TodoItemState]) -> str:
    return "\n".join(f"- [{t.status}] {t.title}" for t in todos)


def _open_counts(todos: list[TodoItemState]) -> str:
    in_progress = sum(1 for t in todos if t.status == "in_progress")
    pending = sum(1 for t in todos if t.status == "pending")
    return f"{in_progress} in_progress, {pending} pending"


def _full_reminder(todos: list[TodoItemState]) -> str:
    lines = [
        "Todo sync check. The Tasks panel the user sees is driven by the "
        "persisted todo list below, and it is stale whenever you finish work "
        "without calling SetTodoList.",
        "",
        "Persisted todo list:",
        _format_list(todos),
        "",
        "- If any status no longer matches reality, call SetTodoList with the "
        "corrected full list now.",
        "- If the list already matches reality exactly, do not call SetTodoList "
        "just to re-assert it.",
        "- From now on, update statuses at every transition: mark an item "
        "in_progress when you begin it, and done the moment it finishes "
        "(one SetTodoList call per transition).",
        "- Before you end your turn, the list must match reality: everything "
        "actually finished is done, and nothing is marked done that is not.",
    ]
    return "\n".join(lines)


def _sparse_reminder(todos: list[TodoItemState]) -> str:
    return (
        f"{_SPARSE_KEY} the persisted todo list still has open items "
        f"({_open_counts(todos)}; full list earlier in context). "
        "Keep statuses matching reality at every transition, and before ending "
        "your turn mark everything actually finished as done via SetTodoList."
    )
