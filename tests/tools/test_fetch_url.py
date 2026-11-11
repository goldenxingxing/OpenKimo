# ruff: noqa

"""Tests for WebFetch tool."""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from typing import Protocol
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from aiohttp import web
from inline_snapshot import snapshot
from kosong.tooling import ToolReturnValue

from kimo_cli.tools.web import fetch as fetch_module
from kimo_cli.tools.web.fetch import FetchURL, Params

_REAL_IS_SAFE_URL = fetch_module._is_safe_url
_REAL_NEW_SESSION = fetch_module.new_client_session


@pytest.fixture(autouse=True)
def _allow_test_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep network tests independent of the SSRF guard and local DNS.

    The guard rejects this environment's fake-IP resolver (198.18.0.0/15 counts as
    private), so the tests run unpinned: the resolver argument is dropped and the
    session falls back to aiohttp's own DNS handling. Pinning itself is covered by
    ``test_pinned_resolver_*`` below.
    """
    monkeypatch.setattr(
        fetch_module,
        "_resolve_safe_addresses",
        lambda url: (urlparse(url).hostname or "localhost", ["127.0.0.1"]),
    )

    def _unpinned_session(**kwargs: object) -> object:
        kwargs.pop("resolver", None)
        return _REAL_NEW_SESSION(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fetch_module, "new_client_session", _unpinned_session)


class MockServerFactory(Protocol):
    async def __call__(
        self,
        response_body: str,
        *,
        content_type: str = "text/html",
        status: int = 200,
    ) -> str: ...


@pytest_asyncio.fixture
async def mock_http_server() -> AsyncIterator[MockServerFactory]:
    """Provide a temporary HTTP server factory that returns static content."""

    runners: list[web.AppRunner] = []

    async def start_server(
        response_body: str,
        *,
        content_type: str = "text/html",
        status: int = 200,
    ) -> str:
        async def handler(request: web.Request) -> web.Response:  # noqa: ARG001
            ct_part, sep, charset_part = content_type.partition(";")
            charset_value: str | None = None
            if sep:
                _, _, charset_value = charset_part.partition("=")
                charset_value = charset_value.strip() or None

            content_type_value = ct_part.strip() or None
            return web.Response(
                text=response_body,
                status=status,
                content_type=content_type_value,
                charset=charset_value,
            )

        app = web.Application()
        app.router.add_get("/", handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="127.0.0.1", port=0)
        await site.start()

        sockets = site._server.sockets  # type: ignore[attr-defined]
        assert sockets, "Server failed to bind to a port."
        port = sockets[0].getsockname()[1]

        runners.append(runner)
        return f"http://127.0.0.1:{port}"

    try:
        yield start_server
    finally:
        for runner in runners:
            await runner.cleanup()


async def test_playwright_fallback_enforces_ssrf_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch_module, "_is_safe_url", _REAL_IS_SAFE_URL)
    result = await FetchURL._fetch_with_playwright(Params(url="http://127.0.0.1:6379/"))
    assert result.is_error
    assert "blocked for security reasons" in result.message


async def test_fetch_url_basic_functionality(
    fetch_url_tool: FetchURL,
    mock_http_server: MockServerFactory,
) -> None:
    """Test basic WebFetch functionality with HTML content extraction."""
    # Use a mocked HTML page to test trafilatura extraction instead of hitting
    # a real external URL.  Real pages change over time and cause flaky failures.
    html_page = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Sample Bug Report</title>
    <meta name="author" content="TestAuthor">
    <meta name="description" content="The default value should be lowercase.">
</head>
<body>
<article>
    <h1>Sample Bug Report</h1>
    <p>The default parameter value for <code>optimizer</code> should probably be
    <code>adamw</code> instead of <code>adamW</code> according to how
    <code>get_optimizer</code> is written.</p>
</article>
</body>
</html>"""
    server_url = await mock_http_server(html_page, content_type="text/html")
    result = await fetch_url_tool(Params(url=server_url))

    assert not result.is_error
    assert isinstance(result.output, str)
    assert (
        result.message == "The returned content is the main text content extracted from the page."
    )
    # Verify trafilatura extracted the meaningful text from the HTML
    assert "optimizer" in result.output
    assert "adamw" in result.output
    assert "adamW" in result.output
    # Verify HTML tags were stripped (not returning raw HTML)
    assert "<article>" not in result.output
    assert "<code>" not in result.output
    # Verify metadata extraction (with_metadata=True)
    assert "title:" in result.output.lower()
    assert "description:" in result.output.lower()


async def test_fetch_url_invalid_url(fetch_url_tool: FetchURL) -> None:
    """Test fetching from an invalid URL."""
    result = await fetch_url_tool(
        Params(url="https://this-domain-definitely-does-not-exist-12345.com/")
    )

    # Should fail with a network error. How depends on the resolver: most
    # answer NXDOMAIN at once, but some networks never answer for a name that
    # does not exist, and the request times out instead.
    assert result.is_error
    assert (
        "Failed to fetch URL due to network error:" in result.message
        or "Failed to fetch URL: request timed out." in result.message
    )


async def test_fetch_url_404_url(fetch_url_tool: FetchURL) -> None:
    """Test fetching from a URL that returns 404."""
    result = await fetch_url_tool(
        Params(url="https://github.com/MoonshotAI/non-existing-repo/issues/1")
    )

    # Should fail with HTTP error
    assert result.is_error
    assert result.message == snapshot(
        "Failed to fetch URL. Status: 404. This may indicate the page is not accessible or the server is down."
    )


async def test_fetch_url_malformed_url(fetch_url_tool: FetchURL) -> None:
    """Test fetching from a malformed URL."""
    result = await fetch_url_tool(Params(url="not-a-valid-url"))

    # Should fail
    assert result.is_error
    assert result.message == snapshot(
        "Failed to fetch URL due to network error: not-a-valid-url. This may indicate the URL is invalid or the server is unreachable."
    )


async def test_fetch_url_empty_url(fetch_url_tool: FetchURL) -> None:
    """Test fetching with empty URL."""
    result = await fetch_url_tool(Params(url=""))

    # Should fail
    assert result.is_error
    assert result.message == snapshot(
        "Failed to fetch URL due to network error: . This may indicate the URL is invalid or the server is unreachable."
    )


async def test_fetch_url_javascript_driven_site(fetch_url_tool: FetchURL) -> None:
    """Test fetching from a JavaScript-driven site that may not work with trafilatura."""
    result = await fetch_url_tool(Params(url="https://www.moonshot.ai/"))

    # This may fail due to JavaScript rendering requirements
    # If it fails, should indicate extraction issues
    if result.is_error:
        assert "failed to extract meaningful content" in result.message.lower()


async def test_fetch_url_mocked_http_responses(
    fetch_url_tool: FetchURL,
    mock_http_server: MockServerFactory,
) -> None:
    """Test fetching multiple mocked HTTP responses."""

    async def mocked_fetch(resp: str, *, content_type: str = "text/html") -> ToolReturnValue:
        server_url = await mock_http_server(resp, content_type=content_type)
        return await fetch_url_tool(Params(url=f"{server_url}/"))

    # plain markdown. Real example: https://lucumr.pocoo.org/2025/10/17/code.md
    plain_markdown = """\
# Title

This is a markdown document.
"""
    result = await mocked_fetch(plain_markdown, content_type="text/markdown; charset=utf-8")
    assert not result.is_error
    assert result.output == snapshot(plain_markdown)
    assert result.message == "The returned content is the full content of the page."

    # Real example: https://langfuse.com/docs.md
    complex_markdown = """\
---
title: Markdown Documentation
description: This is a sample markdown document with front-matter.
---

# Title

This is a markdown document.

<div><p>But has some html</p></div>
"""
    result = await mocked_fetch(
        complex_markdown,
        content_type="text/markdown; charset=utf-8",
    )
    assert not result.is_error
    assert result.output == snapshot(complex_markdown)
    assert result.message == "The returned content is the full content of the page."


async def test_pinned_resolver_answers_only_with_validated_addresses() -> None:
    """The connector must never resolve the name a second time."""
    resolver = fetch_module._PinnedResolver("example.com", ["93.184.216.34"])

    results = await resolver.resolve("example.com", 443, socket.AF_INET)

    assert [entry["host"] for entry in results] == ["93.184.216.34"]
    assert results[0]["family"] == socket.AF_INET
    await resolver.close()


async def test_pinned_resolver_refuses_other_hosts() -> None:
    resolver = fetch_module._PinnedResolver("example.com", ["93.184.216.34"])

    with pytest.raises(OSError, match="unexpected host"):
        await resolver.resolve("evil.test", 443, socket.AF_INET)


async def test_pinned_resolver_refuses_addresses_of_the_wrong_family() -> None:
    resolver = fetch_module._PinnedResolver("example.com", ["93.184.216.34"])

    with pytest.raises(OSError, match="requested family"):
        await resolver.resolve("example.com", 443, socket.AF_INET6)


async def test_http_get_pins_the_validated_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The session must be built with a resolver pinned to the checked IPs."""
    monkeypatch.setattr(
        fetch_module,
        "_resolve_safe_addresses",
        lambda _url: ("example.com", ["93.184.216.34"]),
    )
    captured: dict[str, object] = {}

    def _session(**kwargs: object) -> object:
        captured.update(kwargs)
        raise RuntimeError("stop after capturing the connector")

    monkeypatch.setattr(fetch_module, "new_client_session", _session)

    with pytest.raises(RuntimeError, match="stop after capturing"):
        await FetchURL.fetch_with_http_get(Params(url="http://example.com/"))

    resolver = captured["resolver"]
    assert isinstance(resolver, fetch_module._PinnedResolver)
    assert resolver._addresses == ["93.184.216.34"]


async def test_http_get_blocks_blocked_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_module, "_resolve_safe_addresses", lambda _url: None)

    result = await FetchURL.fetch_with_http_get(Params(url="http://169.254.169.254/latest"))

    assert result.is_error
    assert "blocked for security reasons" in result.message
