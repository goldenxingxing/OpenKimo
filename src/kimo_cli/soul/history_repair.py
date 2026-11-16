"""Put every tool result right after the tool call it answers.

Providers reject a history in which an assistant's tool call is not followed by its
result. The history can end up that way when a worker is killed while writing a
step, or when messages land out of order. Like openclaw, this repairs the history
sent to the provider — a pure function of the history, which is never rewritten —
and a well-formed history comes back unchanged, so the prompt cache is unaffected.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Sequence

from kosong.message import Message, TextPart

MISSING_TOOL_RESULT_TEXT = (
    "The result of this tool call was not recorded; the session was interrupted "
    "before it was saved. Call the tool again if you still need it."
)


def repair_tool_pairing(history: Sequence[Message]) -> list[Message]:
    """`history` with each tool call immediately followed by exactly one result.

    - a result that is elsewhere is moved back after its call;
    - a call with no result gets a synthetic one saying so;
    - a result with no call, or a second result for the same call, is dropped;
    - an id used by more than one call is matched to its results in order
      (openclaw's occurrence queue), not collapsed into one.
    """
    results: defaultdict[str, deque[Message]] = defaultdict(deque)
    for message in history:
        if message.role == "tool" and message.tool_call_id:
            results[message.tool_call_id].append(message)
    calls_per_id: defaultdict[str, int] = defaultdict(int)
    for message in history:
        for tool_call in message.tool_calls or ():
            calls_per_id[tool_call.id] += 1

    repaired: list[Message] = []
    changed = False
    for message in history:
        if message.role == "tool":
            # placed after its call below; anything left over is dropped
            continue
        repaired.append(message)
        for tool_call in message.tool_calls or ():
            calls_per_id[tool_call.id] -= 1
            waiting = results.get(tool_call.id)
            if waiting:
                repaired.append(waiting.popleft())
                if calls_per_id[tool_call.id] == 0:
                    waiting.clear()  # duplicates of the last call's result
            else:
                repaired.append(
                    Message(
                        role="tool",
                        tool_call_id=tool_call.id,
                        content=[TextPart(text=MISSING_TOOL_RESULT_TEXT)],
                    )
                )
                changed = True

    if (
        not changed
        and len(repaired) == len(history)
        and all(a is b for a, b in zip(repaired, history, strict=True))
    ):
        return list(history)
    return repaired
