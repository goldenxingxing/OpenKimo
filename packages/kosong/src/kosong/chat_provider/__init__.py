from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel

from kosong.message import ContentPart, Message, ToolCall, ToolCallPart
from kosong.tooling import Tool

if TYPE_CHECKING:
    import httpx


@runtime_checkable
class ChatProvider(Protocol):
    """The interface of chat providers."""

    name: str
    """
    The name of the chat provider.
    """

    @property
    def model_name(self) -> str:
        """
        The name of the model to use.
        """
        ...

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        """
        The current thinking effort level. Returns None if not explicitly set.
        """
        ...

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StreamedMessage:
        """
        Generate a new message based on the given system prompt, tools, and history.

        Raises:
            APIConnectionError: If the API connection fails.
            APITimeoutError: If the API request times out.
            APIStatusError: If the API returns a status code of 4xx or 5xx.
            ChatProviderError: If any other recognized chat provider error occurs.
        """
        ...

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        """
        Return a copy of self configured with the given thinking effort.
        If the chat provider does not support thinking, simply return a copy of self.
        """
        ...


@runtime_checkable
class RetryableChatProvider(Protocol):
    """Optional interface for providers that can recover from retryable transport errors."""

    def on_retryable_error(self, error: BaseException) -> bool:
        """
        Try to recover provider transport state after a retryable error.

        Returns:
            bool: Whether recovery action was performed.
        """
        ...


type StreamedMessagePart = ContentPart | ToolCall | ToolCallPart


@runtime_checkable
class StreamedMessage(Protocol):
    """The interface of streamed messages."""

    def __aiter__(self) -> AsyncIterator[StreamedMessagePart]:
        """Create an async iterator from the stream."""
        ...

    @property
    def id(self) -> str | None:
        """The ID of the streamed message."""
        ...

    @property
    def usage(self) -> TokenUsage | None:
        """The token usage of the streamed message."""
        ...


type FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "other"]
"""Why the model stopped, in one vocabulary across providers.

Streams may expose it as a ``finish_reason`` property once iteration ends; it is
not part of the `StreamedMessage` protocol so that streams predating it still
conform, and callers read it with ``getattr(stream, "finish_reason", None)``.
``None`` means the provider did not say.
"""

_FINISH_REASONS: dict[str, FinishReason] = {
    # OpenAI chat completions and Moonshot
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
    # Anthropic
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
    # Google
    "finish_reason_unspecified": "other",
    "safety": "content_filter",
    "recitation": "content_filter",
    "blocklist": "content_filter",
    "prohibited_content": "content_filter",
    "spii": "content_filter",
    # OpenAI responses (``incomplete_details.reason``)
    "max_output_tokens": "length",
}


def normalize_finish_reason(raw: str | None) -> FinishReason | None:
    """Map a provider's stop reason onto `FinishReason`; unknown values are ``"other"``."""
    if not raw:
        return None
    return _FINISH_REASONS.get(raw.lower(), "other")


class TokenUsage(BaseModel):
    """Token usage statistics."""

    input_other: int
    """Input tokens excluding `input_cache_read` and `input_cache_creation`."""
    output: int
    """Total output tokens."""
    input_cache_read: int = 0
    """Cached input tokens."""
    input_cache_creation: int = 0
    """Input tokens used for cache creation. For now, only Anthropic API supports this."""
    reasoning_output: int | None = None
    """Output tokens spent on reasoning, when the backend reports it.

    Only some providers expose this (e.g. Moonshot reports
    ``completion_tokens_details.reasoning_tokens``). ``None`` means "unknown",
    which callers must not treat as "no reasoning happened"."""

    @property
    def total(self) -> int:
        """Total tokens used, including input and output tokens."""
        return self.input + self.output

    @property
    def input(self) -> int:
        """Total input tokens, including cached and uncached tokens."""
        return self.input_other + self.input_cache_read + self.input_cache_creation


type ThinkingEffort = Literal["off", "low", "medium", "high", "xhigh", "max"]
"""The effort level for thinking.

Support for levels above ``high`` varies by provider:

- **Anthropic**: ``xhigh`` is accepted only on Claude Opus 4.7; ``max`` is
  accepted on Mythos, Opus 4.7/4.6, and Sonnet 4.6. Unsupported levels are
  clamped down to ``high``.
- **OpenAI**: ``xhigh`` is accepted natively for reasoning-capable models
  after ``gpt-5.1-codex-max`` and passes through unchanged. ``max`` is
  Anthropic-specific and clamps to ``xhigh`` (OpenAI's ceiling).
- **Kimo**: requests only serialize thinking as enabled or disabled; the
  caller-provided effort remains unchanged as provider state.
- **Gemini**: ``xhigh`` and ``max`` clamp to ``high`` (no native support).
"""


class ChatProviderError(Exception):
    """The error raised by a chat provider."""

    def __init__(self, message: str):
        super().__init__(message)


class APIConnectionError(ChatProviderError):
    """The error raised when the API connection fails."""


class APITimeoutError(ChatProviderError):
    """The error raised when the API request times out."""


class APIStatusError(ChatProviderError):
    """The error raised when the API returns a status code of 4xx or 5xx."""

    status_code: int
    request_id: str | None
    trace_id: str | None
    retry_after: float | None

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.trace_id = trace_id
        self.retry_after = retry_after
        """Seconds the server asked the caller to wait, when it said so."""


class APIEmptyResponseError(ChatProviderError):
    """The error raised when the API returns an empty response."""


def convert_httpx_error(error: httpx.HTTPError) -> ChatProviderError:
    """Convert an httpx transport error to the corresponding ChatProviderError.

    This is a shared utility for all chat providers. SDK-specific exceptions
    (e.g. AnthropicError, OpenAIError) should be handled by each provider's
    own conversion logic; only raw httpx exceptions that leak through
    (typically during streaming) should be routed here.
    """
    import httpx

    if isinstance(error, httpx.TimeoutException):
        return APITimeoutError(str(error))
    if isinstance(error, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return APIConnectionError(str(error))
    if isinstance(error, httpx.HTTPStatusError):
        req_id = error.response.headers.get("x-request-id")
        trace_id = error.response.headers.get("x-trace-id")
        return APIStatusError(
            error.response.status_code,
            str(error),
            request_id=req_id,
            trace_id=trace_id,
            retry_after=parse_retry_after(error.response.headers),
        )
    return ChatProviderError(f"HTTP error: {error}")


def parse_retry_after(headers: Mapping[str, Any] | None) -> float | None:
    """Read the delay a server asked the caller to wait out, in seconds.

    Recognizes ``retry-after-ms`` (milliseconds; non-standard but sent by
    several providers) and both RFC 9110 forms of ``Retry-After``: a delay in
    seconds, or an HTTP-date. A date already in the past yields ``0.0``.

    Returns:
        The requested delay, or ``None`` when the headers don't ask for one.
    """
    if headers is None:
        return None

    def get(name: str) -> str | None:
        # httpx.Headers and the SDKs' header mappings look up
        # case-insensitively; a plain dict does not, hence the scan.
        value = headers.get(name)
        if value is None:
            for key, candidate in headers.items():
                if key.lower() == name:
                    value = candidate
                    break
        return None if value is None else str(value).strip()

    if (raw_ms := get("retry-after-ms")) is not None:
        try:
            return max(0.0, float(raw_ms) / 1000.0)
        except ValueError:
            pass

    raw = get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())
