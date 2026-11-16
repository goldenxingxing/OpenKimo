from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, NamedTuple, Protocol, cast, runtime_checkable

import kosong
from kosong.chat_provider import TokenUsage
from kosong.message import Message
from kosong.tooling.empty import EmptyToolset

import kimo_cli.prompts as prompts
from kimo_cli.llm import LLM
from kimo_cli.soul.message import system
from kimo_cli.utils.logging import logger
from kimo_cli.wire.types import ContentPart, ImageURLPart, TextPart, ThinkPart

COMPACTION_SYSTEM_PROMPT = "You are a helpful assistant that compacts conversation context."
COMPACTION_OUTPUT_PREFIX = "Previous context has been compacted. Here is the compaction output:"


class CompactionResult(NamedTuple):
    messages: Sequence[Message]
    usage: TokenUsage | None
    trace_id: str | None = None

    @property
    def estimated_token_count(self) -> int:
        """Estimate the token count of the compacted messages.

        When LLM usage is available, ``usage.output`` gives the exact token count
        of the generated summary (the first message).  Preserved messages (all
        subsequent messages) are estimated from their text length.

        When usage is not available (no compaction LLM call was made), all
        messages are estimated from text length.

        The estimate is intentionally conservative — it will be replaced by the
        real value on the next LLM call.
        """
        if self.usage is not None and len(self.messages) > 0:
            summary_tokens = self.usage.output
            preserved_tokens = estimate_text_tokens(self.messages[1:])
            return summary_tokens + preserved_tokens

        return estimate_text_tokens(self.messages)


def estimate_text_tokens(messages: Sequence[Message]) -> int:
    """Estimate tokens from message text content using a character-based heuristic."""
    total_chars = 0
    for msg in messages:
        for part in msg.content:
            if isinstance(part, TextPart):
                total_chars += len(part.text)
    # ~4 chars per token for English; somewhat underestimates for CJK text,
    # but this is a temporary estimate that gets corrected on the next LLM call.
    return total_chars // 4


def estimate_message_bytes(messages: Sequence[Message]) -> int:
    """Estimate the serialized request-body size (in bytes) of ``messages``.

    The provider enforces a hard request-body byte limit (2 MiB on Moonshot)
    that token-based estimates miss entirely: base64 media, thinking blocks,
    and tool-call arguments count for ~0 tokens but dominate the byte size.
    This estimate sums the UTF-8 byte length of every serialized part so
    byte-heavy content (especially images) is accounted for. Approximation
    within ~10% is intentional — the goal is the right order of magnitude.
    """
    total = 0
    for msg in messages:
        total += len(msg.role.encode("utf-8"))
        if msg.name:
            total += len(msg.name.encode("utf-8"))
        if msg.tool_call_id:
            total += len(msg.tool_call_id.encode("utf-8"))
        for part in msg.content:
            if isinstance(part, TextPart):
                total += len(part.text.encode("utf-8"))
            elif isinstance(part, ThinkPart):
                total += len(part.think.encode("utf-8"))
                if part.encrypted:
                    total += len(part.encrypted.encode("utf-8"))
            elif isinstance(part, ImageURLPart):
                # base64 data URL body is the dominant term for images
                total += len(part.image_url.url.encode("utf-8"))
            else:
                total += len(part.model_dump_json(exclude_none=True).encode("utf-8"))
        for tool_call in msg.tool_calls or ():
            total += len(tool_call.id.encode("utf-8"))
            total += len(tool_call.function.name.encode("utf-8"))
            if tool_call.function.arguments:
                total += len(tool_call.function.arguments.encode("utf-8"))
            if tool_call.extras:
                total += len(json.dumps(tool_call.extras, ensure_ascii=False).encode("utf-8"))
    return total


PRESERVED_TAIL_MIN_TOKENS = 2_000
PRESERVED_TAIL_MAX_TOKENS = 15_000


def preserved_tail_budget(max_context_size: int) -> int:
    """Tokens of recent history compaction keeps verbatim: a quarter of the window,
    between 2k and 15k (opencode's ``preserveRecentBudget``). The upper bound when
    the window is unknown."""
    if max_context_size <= 0:
        return PRESERVED_TAIL_MAX_TOKENS
    return max(PRESERVED_TAIL_MIN_TOKENS, min(PRESERVED_TAIL_MAX_TOKENS, max_context_size // 4))


def should_auto_compact(
    token_count: int,
    max_context_size: int,
    *,
    trigger_ratio: float,
    reserved_context_size: int,
    request_bytes: int | None = None,
    max_request_bytes: int | None = None,
) -> bool:
    """Determine whether auto-compaction should be triggered.

    Returns True when any condition is met (whichever fires first):
    - Ratio-based: token_count >= max_context_size * trigger_ratio
    - Reserved-based: token_count + reserved_context_size >= max_context_size
    - Bytes-based: request_bytes > max_request_bytes (when both are provided).
      The provider rejects request bodies over a hard byte limit regardless of
      token count, so byte-heavy histories (e.g. base64 images) must compact
      even when the token estimate is still low.

    A ``max_context_size`` of zero or less means the window is unknown.
    Only the byte limit applies then: the
    token conditions would hold for any count and compact on every step.
    """
    if (
        request_bytes is not None
        and max_request_bytes is not None
        and request_bytes > max_request_bytes
    ):
        return True
    if max_context_size <= 0:
        return False
    return (
        token_count >= max_context_size * trigger_ratio
        or token_count + reserved_context_size >= max_context_size
    )


@runtime_checkable
class Compaction(Protocol):
    async def compact(
        self,
        messages: Sequence[Message],
        llm: LLM,
        *,
        custom_instruction: str = "",
    ) -> CompactionResult:
        """
        Compact a sequence of messages into a new sequence of messages.

        Args:
            messages (Sequence[Message]): The messages to compact.
            llm (LLM): The LLM to use for compaction.
            custom_instruction: Optional user instruction to guide compaction focus.
        Returns:
            CompactionResult: The compacted messages and token usage from the compaction LLM call.

        Raises:
            ChatProviderError: When the chat provider returns an error.
        """
        ...


if TYPE_CHECKING:

    def type_check(simple: SimpleCompaction):
        _: Compaction = simple


class SimpleCompaction:
    def __init__(self, max_preserved_messages: int = 2) -> None:
        self.max_preserved_messages = max_preserved_messages

    async def compact(
        self,
        messages: Sequence[Message],
        llm: LLM,
        *,
        custom_instruction: str = "",
    ) -> CompactionResult:
        compact_message, to_preserve = self.prepare(
            messages,
            custom_instruction=custom_instruction,
            max_context_size=llm.max_context_size,
        )
        if compact_message is None:
            return CompactionResult(messages=to_preserve, usage=None)

        logger.debug("Compacting context...")
        result = await kosong.step(
            chat_provider=llm.chat_provider,
            system_prompt=COMPACTION_SYSTEM_PROMPT,
            toolset=EmptyToolset(),
            history=[compact_message],
        )
        if result.usage:
            logger.debug(
                "Compaction used {input} input tokens and {output} output tokens",
                input=result.usage.input,
                output=result.usage.output,
            )

        content: list[ContentPart] = [system(COMPACTION_OUTPUT_PREFIX)]
        compacted_msg = result.message

        # drop thinking parts if any
        content.extend(part for part in compacted_msg.content if not isinstance(part, ThinkPart))
        to_compact = list(messages)[: len(messages) - len(to_preserve)]
        if details := _carried_details(to_compact, to_preserve):
            content.append(TextPart(text=details))
        compacted_messages: list[Message] = [Message(role="user", content=content)]
        compacted_messages.extend(to_preserve)
        return CompactionResult(
            messages=compacted_messages, usage=result.usage, trace_id=result.trace_id
        )

    class PrepareResult(NamedTuple):
        compact_message: Message | None
        to_preserve: Sequence[Message]

    def prepare(
        self,
        messages: Sequence[Message],
        *,
        custom_instruction: str = "",
        max_context_size: int | None = None,
    ) -> PrepareResult:
        """Split `messages` into what to summarise and the recent tail to keep.

        With `max_context_size`, the tail is the most recent messages that fit in
        `preserved_tail_budget`. Without it — and when the whole history fits in the
        budget, so that compacting still compacts something — the tail is the last
        `max_preserved_messages` user/assistant messages.
        """
        if not messages or self.max_preserved_messages <= 0:
            return self.PrepareResult(compact_message=None, to_preserve=messages)

        history = list(messages)
        if max_context_size is not None:
            start = _budgeted_tail_start(history, preserved_tail_budget(max_context_size))
            if start is not None:
                return self._build(history, start, custom_instruction)

        preserve_start_index = len(history)
        n_preserved = 0
        for index in range(len(history) - 1, -1, -1):
            if history[index].role in {"user", "assistant"}:
                n_preserved += 1
                if n_preserved == self.max_preserved_messages:
                    preserve_start_index = index
                    break

        if n_preserved < self.max_preserved_messages:
            return self.PrepareResult(compact_message=None, to_preserve=messages)

        return self._build(history, preserve_start_index, custom_instruction)

    def _build(
        self, history: list[Message], preserve_start_index: int, custom_instruction: str
    ) -> PrepareResult:
        to_compact = history[:preserve_start_index]
        to_preserve = history[preserve_start_index:]

        if not to_compact:
            # Let's hope this won't exceed the context size limit
            return self.PrepareResult(compact_message=None, to_preserve=to_preserve)

        # Create input message for compaction
        compact_message = Message(role="user", content=[])
        for i, msg in enumerate(to_compact):
            compact_message.content.append(
                TextPart(text=f"## Message {i + 1}\nRole: {msg.role}\nContent:\n")
            )
            compact_message.content.extend(
                part for part in msg.content if isinstance(part, TextPart)
            )
        prompt_text = "\n" + prompts.COMPACT
        if custom_instruction:
            prompt_text += (
                "\n\n**User's Custom Compaction Instruction:**\n"
                "The user has specifically requested the following focus during compaction. "
                "You MUST prioritize this instruction above the default compression priorities:\n"
                f"{custom_instruction}"
            )
        compact_message.content.append(TextPart(text=prompt_text))
        return self.PrepareResult(compact_message=compact_message, to_preserve=to_preserve)


def _budgeted_tail_start(history: list[Message], budget_tokens: int) -> int | None:
    """Index where the kept tail starts, or ``None`` when all of `history` fits.

    The tail may only start at a user or assistant message, so that a tool call is
    never kept without its result or the other way round. When even the newest such
    message and what follows it exceed the budget, the tail is just that group.
    """
    tail_tokens = 0
    start: int | None = None
    newest_boundary: int | None = None
    for index in range(len(history) - 1, -1, -1):
        tail_tokens += estimate_message_bytes([history[index]]) // 4
        if history[index].role not in {"user", "assistant"}:
            continue
        if newest_boundary is None:
            newest_boundary = index
        if tail_tokens > budget_tokens:
            break
        start = index
    else:
        return None  # everything fits
    return start if start is not None else newest_boundary


LATEST_USER_REQUEST_MAX_CHARS = 800
"""How much of the latest user request a summary carries when it is compacted away."""

_FILE_LIST_MAX = 100
_FILE_READ_TOOLS = frozenset({"ReadFile", "ReadMediaFile"})
_FILE_WRITE_TOOLS = frozenset({"WriteFile", "StrReplaceFile"})
_DETAIL_BLOCK = re.compile(r"<(files_read|files_modified|latest_user_request)>\n(.*?)\n</\1>", re.S)


def _user_request_text(message: Message) -> str:
    """The text a person typed, leaving out injected reminders, notifications and
    earlier compaction summaries (user messages too, but not written by a person)."""
    if message.role != "user" or _is_compaction_summary(message):
        return ""
    return "\n".join(
        part.text
        for part in message.content
        if isinstance(part, TextPart)
        and part.text.strip()
        and not part.text.lstrip().startswith(("<system", "<notification"))
    ).strip()


_COMPACTION_SUMMARY_MARKER = f"<system>{COMPACTION_OUTPUT_PREFIX}</system>"
"""The first part of a summary message, as written by `system(COMPACTION_OUTPUT_PREFIX)`."""


def _is_compaction_summary(message: Message) -> bool:
    first = message.content[0] if message.content else None
    return isinstance(first, TextPart) and first.text == _COMPACTION_SUMMARY_MARKER


def _tool_call_path(arguments: str | None) -> str | None:
    try:
        parsed: object = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    path = cast(dict[str, object], parsed).get("path")
    return path if isinstance(path, str) and path else None


def _carried_details(to_compact: Sequence[Message], to_preserve: Sequence[Message]) -> str:
    """Blocks appended to a summary, computed from the messages rather than recalled
    by the model: the files read and modified — merged with those listed by an
    earlier summary among `to_compact` — and the latest user request when it is
    being compacted away.
    """
    read: dict[str, None] = {}
    modified: dict[str, None] = {}
    carried_request = ""

    def _mark(files: dict[str, None], path: str) -> None:
        files.pop(path, None)
        files[path] = None  # most recent last

    for message in to_compact:
        for part in message.content:
            if not isinstance(part, TextPart):
                continue
            for kind, body in _DETAIL_BLOCK.findall(part.text):
                if kind == "latest_user_request":
                    carried_request = body
                    continue
                for line in body.splitlines():
                    if line.startswith("- "):
                        _mark(read if kind == "files_read" else modified, line[2:])
        for tool_call in message.tool_calls or ():
            name = tool_call.function.name
            if name not in _FILE_READ_TOOLS | _FILE_WRITE_TOOLS:
                continue
            if (path := _tool_call_path(tool_call.function.arguments)) is None:
                continue
            if name in _FILE_WRITE_TOOLS:
                _mark(modified, path)
                read.pop(path, None)
            elif path not in modified:
                _mark(read, path)

    blocks: list[str] = []
    for tag, files in (("files_read", read), ("files_modified", modified)):
        if files:
            listed = "\n".join(f"- {path}" for path in list(files)[-_FILE_LIST_MAX:])
            blocks.append(f"<{tag}>\n{listed}\n</{tag}>")

    if not any(_user_request_text(m) for m in to_preserve):
        request = next(
            (text for m in reversed(to_compact) if (text := _user_request_text(m))),
            carried_request,
        )
        if len(request) > LATEST_USER_REQUEST_MAX_CHARS:
            request = request[:LATEST_USER_REQUEST_MAX_CHARS] + "…"
        if request:
            blocks.append(f"<latest_user_request>\n{request}\n</latest_user_request>")

    return "\n".join(blocks)
