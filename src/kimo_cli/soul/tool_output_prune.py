"""Clear old tool outputs from the request, in batches, without touching the history.

Tool outputs are most of a long session's context, and they never shrank: only a
full compaction, an LLM call that loses detail, got rid of them. Following opencode,
outputs older than the most recent `protect_tokens` of tool output are replaced by a
short placeholder once there are at least `minimum_tokens` of them.

The replacement happens in batches and a cleared output stays cleared, so the request
prefix is identical from one step to the next between batches and the provider's
prefix cache keeps hitting. Clearing whatever falls out of a sliding window at every
step would change the prefix every step. The context file keeps the full outputs;
this is a view of the history for the request, and only compaction rewrites history.
"""

from __future__ import annotations

from collections.abc import Sequence

from kosong.message import Message, TextPart

from kimo_cli.soul.compaction import estimate_message_bytes


def _tokens(message: Message) -> int:
    return estimate_message_bytes([message]) // 4


class ToolOutputPruner:
    def __init__(self, *, protect_tokens: int = 40_000, minimum_tokens: int = 20_000) -> None:
        self.protect_tokens = protect_tokens
        self.minimum_tokens = minimum_tokens
        self._cleared: set[str] = set()

    @property
    def cleared(self) -> frozenset[str]:
        """Tool call ids whose output is cleared from requests."""
        return frozenset(self._cleared)

    def update(self, history: Sequence[Message]) -> bool:
        """Clear the next batch if it is due. Returns whether anything was cleared."""
        protected = 0
        due: list[str] = []
        due_tokens = 0
        for message in reversed(history):
            if message.role != "tool" or not message.tool_call_id:
                continue
            tokens = _tokens(message)
            if protected < self.protect_tokens:
                protected += tokens
                continue
            if message.tool_call_id not in self._cleared:
                due.append(message.tool_call_id)
                due_tokens += tokens
        if due and due_tokens >= self.minimum_tokens:
            self._cleared.update(due)
            return True
        return False

    def view(self, history: Sequence[Message]) -> list[Message]:
        """`history` with the cleared outputs replaced by a placeholder."""
        if not self._cleared:
            return list(history)
        tool_names = {
            tool_call.id: tool_call.function.name
            for message in history
            for tool_call in message.tool_calls or ()
        }
        return [
            _placeholder(message, tool_names.get(message.tool_call_id, "tool"))
            if message.role == "tool" and message.tool_call_id in self._cleared
            else message
            for message in history
        ]


def _placeholder(message: Message, tool_name: str) -> Message:
    size = estimate_message_bytes([message])
    return message.model_copy(
        update={
            "content": [
                TextPart(
                    text=(
                        f"[Old {tool_name} output cleared to save context ({size} bytes). "
                        "Run the tool again if you need it.]"
                    )
                )
            ]
        }
    )
