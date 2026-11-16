"""Tests for ``Retry-After`` parsing and its propagation onto ``APIStatusError``."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx

from kosong.chat_provider import APIStatusError, convert_httpx_error, parse_retry_after


class TestParseRetryAfter:
    def test_none_headers(self) -> None:
        assert parse_retry_after(None) is None

    def test_absent_header(self) -> None:
        assert parse_retry_after({"content-type": "application/json"}) is None

    def test_delay_seconds(self) -> None:
        assert parse_retry_after({"retry-after": "30"}) == 30.0

    def test_fractional_seconds(self) -> None:
        assert parse_retry_after({"retry-after": "1.5"}) == 1.5

    def test_surrounding_whitespace(self) -> None:
        assert parse_retry_after({"retry-after": " 12 "}) == 12.0

    def test_milliseconds_variant_wins(self) -> None:
        headers = {"retry-after": "30", "retry-after-ms": "1500"}
        assert parse_retry_after(headers) == 1.5

    def test_case_insensitive_on_plain_dict(self) -> None:
        assert parse_retry_after({"Retry-After": "7"}) == 7.0

    def test_http_date_in_the_future(self) -> None:
        when = datetime.now(UTC) + timedelta(seconds=60)
        parsed = parse_retry_after({"retry-after": format_datetime(when, usegmt=True)})
        assert parsed is not None
        # Second-resolution formatting plus test runtime; a loose band is enough.
        assert 55.0 <= parsed <= 61.0

    def test_http_date_in_the_past_is_zero(self) -> None:
        when = datetime.now(UTC) - timedelta(hours=1)
        assert parse_retry_after({"retry-after": format_datetime(when, usegmt=True)}) == 0.0

    def test_negative_seconds_clamped_to_zero(self) -> None:
        assert parse_retry_after({"retry-after": "-5"}) == 0.0

    def test_unparsable_value(self) -> None:
        assert parse_retry_after({"retry-after": "soon"}) is None

    def test_unparsable_milliseconds_falls_back_to_seconds(self) -> None:
        assert parse_retry_after({"retry-after-ms": "soon", "retry-after": "3"}) == 3.0

    def test_httpx_headers_object(self) -> None:
        headers = httpx.Headers({"Retry-After": "20"})
        assert parse_retry_after(headers) == 20.0


class TestConvertHttpxErrorCarriesRetryAfter:
    def _status_error(self, headers: dict[str, str]) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", "https://api.test/v1/chat/completions")
        response = httpx.Response(429, headers=headers, request=request)
        return httpx.HTTPStatusError("rate limited", request=request, response=response)

    def test_retry_after_propagated(self) -> None:
        converted = convert_httpx_error(self._status_error({"retry-after": "42"}))

        assert isinstance(converted, APIStatusError)
        assert converted.status_code == 429
        assert converted.retry_after == 42.0

    def test_absent_retry_after_is_none(self) -> None:
        converted = convert_httpx_error(self._status_error({"x-request-id": "abc"}))

        assert isinstance(converted, APIStatusError)
        assert converted.request_id == "abc"
        assert converted.retry_after is None

    def test_default_is_none_when_not_supplied(self) -> None:
        assert APIStatusError(500, "boom").retry_after is None
