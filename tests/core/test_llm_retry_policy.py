"""Tests for the LLM retry policy: what is retried, and how long the loop waits.

Covers:
- ``KimoSoul._is_retryable_error`` / ``is_retryable_api_error``: the shared status table
- ``_retry_wait``: ``Retry-After`` precedence, its ceiling, and exponential fallback
- ``_is_context_overflow_error``: token overflow vs. the byte-size rejection
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from kosong.chat_provider import (
    APIConnectionError,
    APIEmptyResponseError,
    APIStatusError,
    APITimeoutError,
    ChatProviderError,
)
from tenacity import RetryCallState

from kimo_cli.soul.kimosoul import (
    _RETRY_AFTER_CAP_S,
    _RETRY_WAIT_MAX_S,
    KimoSoul,
    _is_context_overflow_error,
    _retry_wait,
    is_retryable_api_error,
    is_retryable_llm_error,
    is_retryable_status_code,
)


def _state(error: BaseException | None = None, attempt_number: int = 1) -> RetryCallState:
    state = RetryCallState(retry_object=cast(Any, None), fn=None, args=(), kwargs={})
    state.attempt_number = attempt_number
    if error is not None:
        state.set_exception((type(error), error, error.__traceback__))
    return state


class TestIsRetryableError:
    """The loop and the telemetry field must agree on status codes — they used to not."""

    @pytest.mark.parametrize(
        "status_code",
        # 520-527 is the Cloudflare family (origin unreachable, handshake
        # failed, …) that an explicit whitelist of 5xx codes kept missing.
        [408, 409, 429, 500, 502, 503, 504, 507, 520, 522, 524, 529, 599],
    )
    def test_transient_status_codes_are_retried(self, status_code: int) -> None:
        error = APIStatusError(status_code, f"status {status_code}")
        assert is_retryable_llm_error(error)
        assert is_retryable_api_error(error)

    @pytest.mark.parametrize("status_code", [400, 401, 402, 403, 404, 422])
    def test_client_errors_are_not_retried(self, status_code: int) -> None:
        error = APIStatusError(status_code, f"status {status_code}")
        assert not is_retryable_llm_error(error)
        assert not is_retryable_api_error(error)

    @pytest.mark.parametrize("status_code", [501, 505])
    def test_permanent_server_errors_are_not_retried(self, status_code: int) -> None:
        """A permanent refusal of the request itself won't change on a retry."""
        error = APIStatusError(status_code, f"status {status_code}")
        assert not is_retryable_llm_error(error)
        assert not is_retryable_api_error(error)

    @pytest.mark.parametrize(
        "error",
        [
            APIConnectionError("connection reset"),
            APITimeoutError("timed out"),
            APIEmptyResponseError("empty"),
        ],
        ids=["connection", "timeout", "empty"],
    )
    def test_transport_failures_are_retried(self, error: ChatProviderError) -> None:
        assert is_retryable_llm_error(error)
        assert is_retryable_api_error(error)

    @pytest.mark.parametrize(
        "error",
        [
            TimeoutError("timed out"),
            ConnectionResetError("reset by peer"),
            BrokenPipeError("broken pipe"),
        ],
        ids=["builtin-timeout", "connection-reset", "broken-pipe"],
    )
    def test_builtin_transport_failures_are_retried(self, error: OSError) -> None:
        """asyncio and socket raise these directly when a connection dies mid-flight."""
        assert is_retryable_llm_error(error)

    def test_unmapped_provider_error_is_reported_but_not_re_sent(self) -> None:
        """Reporting an unmapped error as transient is not a reason to retry it blind."""
        error = ChatProviderError("an error nobody mapped")
        assert is_retryable_api_error(error)
        assert not is_retryable_llm_error(error)

    def test_connection_error_is_retried_regardless_of_recovery(self) -> None:
        """Recovery failing no longer makes the error unretryable."""
        error = APIConnectionError("connection reset")
        error._kimo_recovery_exhausted = True  # type: ignore[attr-defined]  # legacy marker
        assert is_retryable_llm_error(error)

    def test_soul_predicate_delegates(self) -> None:
        assert KimoSoul._is_retryable_error(APIStatusError(529, "overloaded"))
        assert not KimoSoul._is_retryable_error(APIStatusError(400, "bad request"))

    def test_unrelated_exception_is_not_retried(self) -> None:
        assert not is_retryable_llm_error(RuntimeError("bug"))
        assert not is_retryable_api_error(RuntimeError("bug"))


class TestIsRetryableStatusCode:
    """The rule is a range, not a list, so unseen 5xx codes are covered by default."""

    def test_whole_5xx_range_except_permanent(self) -> None:
        retryable = {code for code in range(500, 600) if is_retryable_status_code(code)}
        assert retryable == set(range(500, 600)) - {501, 505}

    @pytest.mark.parametrize("status_code", [200, 201, 301, 399, 499, 600, 0])
    def test_non_error_and_out_of_range_codes(self, status_code: int) -> None:
        assert not is_retryable_status_code(status_code)

    def test_only_timing_related_4xx(self) -> None:
        retryable = {code for code in range(400, 500) if is_retryable_status_code(code)}
        assert retryable == {408, 409, 429}


class TestRetryWait:
    def test_server_retry_after_wins(self) -> None:
        error = APIStatusError(429, "slow down", retry_after=12.0)
        assert _retry_wait(_state(error)) == 12.0

    def test_server_retry_after_is_capped(self) -> None:
        error = APIStatusError(429, "slow down", retry_after=600.0)
        assert _retry_wait(_state(error)) == _RETRY_AFTER_CAP_S

    def test_zero_retry_after_falls_back_to_backoff(self) -> None:
        """Taken literally, zero would spend every remaining attempt back to back."""
        error = APIStatusError(429, "slow down", retry_after=0.0)
        assert 0.5 <= _retry_wait(_state(error)) <= 1.0

    def test_falls_back_to_backoff_without_retry_after(self) -> None:
        wait = _retry_wait(_state(APIStatusError(503, "unavailable"), attempt_number=1))
        # initial 0.5 s plus up to 0.5 s of jitter
        assert 0.5 <= wait <= 1.0

    def test_backoff_grows_with_attempts(self) -> None:
        early = _retry_wait(_state(APIStatusError(503, "unavailable"), attempt_number=1))
        later = _retry_wait(_state(APIStatusError(503, "unavailable"), attempt_number=5))
        assert later > early
        assert 8.0 <= later <= 8.5

    def test_backoff_is_capped(self) -> None:
        wait = _retry_wait(_state(APIStatusError(503, "unavailable"), attempt_number=20))
        assert wait == _RETRY_WAIT_MAX_S

    def test_non_numeric_retry_after_is_ignored(self) -> None:
        error = APIStatusError(429, "slow down")
        error.retry_after = cast(Any, "soon")
        assert 0.5 <= _retry_wait(_state(error)) <= 1.0

    def test_bool_retry_after_is_ignored(self) -> None:
        """``True`` is an int in Python; it is not a one-second wait."""
        error = APIStatusError(429, "slow down")
        error.retry_after = cast(Any, True)
        assert 0.5 <= _retry_wait(_state(error)) <= 1.0

    def test_connection_error_uses_backoff(self) -> None:
        assert 0.5 <= _retry_wait(_state(APIConnectionError("reset"))) <= 1.0

    def test_missing_outcome_uses_backoff(self) -> None:
        assert 0.5 <= _retry_wait(_state()) <= 1.0


class TestIsContextOverflowError:
    @pytest.mark.parametrize(
        "message",
        [
            "context length exceeded",
            "This model's maximum context length is 262144 tokens",
            "too many tokens in the request",
            # Kimo / Moonshot
            "Invalid request: Your request exceeded model token limit: 262144",
            # Anthropic
            "prompt is too long: 208310 tokens > 200000 maximum",
            # Gemini
            "The input token count (1200000) exceeds the maximum number of tokens allowed "
            "(1048576).",
            # OpenAI Responses
            "Your input exceeds the context window of this model.",
        ],
    )
    def test_token_overflow_recognized(self, message: str) -> None:
        assert _is_context_overflow_error(APIStatusError(400, message))

    def test_bad_max_tokens_setting_is_not_overflow(self) -> None:
        """Compaction cannot fix a max_tokens value the model rejects."""
        error = APIStatusError(400, "Invalid 'max tokens' value: must be <= 8192")
        assert not _is_context_overflow_error(error)

    def test_byte_size_rejection_left_to_the_other_self_heal(self) -> None:
        """The two self-heals are one-shot each, so their triggers must be disjoint."""
        error = APIStatusError(400, "total message size 3000000 exceeds limit 2097152")
        assert not _is_context_overflow_error(error)

    def test_other_client_error_not_overflow(self) -> None:
        assert not _is_context_overflow_error(APIStatusError(400, "invalid api key"))

    def test_server_error_not_overflow(self) -> None:
        assert not _is_context_overflow_error(APIStatusError(500, "context length exceeded"))

    def test_non_status_error_not_overflow(self) -> None:
        assert not _is_context_overflow_error(RuntimeError("context length exceeded"))
