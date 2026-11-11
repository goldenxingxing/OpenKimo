"""Notice an agent going round in circles.

The toolset already stops a turn when the same call is made in many consecutive
steps. An agent alternating between calls (A, B, A, B) or repeating a failing command
between reads of other files resets that count every step. This counts, over the last
`window` steps, the separate runs of each call that got the same result, the way
crush identifies a call with its result: a call that keeps returning something new is
making progress and never counts.

Repeats in consecutive steps form one run and are left to the toolset's own reminders.
Numbers differ from crush (5 repeats in 10 steps): a strict two-call cycle reaches only
5 runs of each in 10 steps, so the window here is 12.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Sequence

from kosong.message import ToolCall
from kosong.tooling import ToolResult

LOOP_WINDOW_STEPS = 12
LOOP_MIN_RUNS = 6

POLLING_TOOLS = frozenset({"TaskOutput", "TaskList"})
"""Tools meant to be called again until something changes; never counted."""


def _canonical_arguments(arguments: str | None) -> str:
    try:
        return json.dumps(json.loads(arguments or "{}"), sort_keys=True, ensure_ascii=False)
    except json.JSONDecodeError:
        return arguments or ""


def call_signature(tool_call: ToolCall, result: ToolResult) -> str:
    value = result.return_value
    output = (
        value.output
        if isinstance(value.output, str)
        else json.dumps([part.model_dump(mode="json") for part in value.output], sort_keys=True)
    )
    material = json.dumps(
        [
            tool_call.function.name,
            _canonical_arguments(tool_call.function.arguments),
            value.is_error,
            value.message,
            output,
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class LoopDetector:
    def __init__(self, *, window: int = LOOP_WINDOW_STEPS, min_runs: int = LOOP_MIN_RUNS) -> None:
        self.window = window
        self.min_runs = min_runs
        self._steps: deque[frozenset[str]] = deque(maxlen=window)

    def reset(self) -> None:
        self._steps.clear()

    def record_step(self, tool_calls: Sequence[ToolCall], results: Sequence[ToolResult]) -> bool:
        """Add a step's calls and results; returns whether the agent is going in circles."""
        by_id = {result.tool_call_id: result for result in results}
        signatures = frozenset(
            call_signature(tool_call, by_id[tool_call.id])
            for tool_call in tool_calls
            if tool_call.id in by_id and tool_call.function.name not in POLLING_TOOLS
        )
        self._steps.append(signatures)
        return any(self._runs(signature) >= self.min_runs for signature in signatures)

    def _runs(self, signature: str) -> int:
        runs = 0
        previous = False
        for step in self._steps:
            present = signature in step
            if present and not previous:
                runs += 1
            previous = present
        return runs
