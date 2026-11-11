import asyncio
import ipaddress
import os
import socket
from pathlib import Path
from typing import override
from urllib.parse import urlparse

import aiohttp
import trafilatura
from kosong.tooling import CallableTool2, ToolReturnValue
from pydantic import BaseModel, Field

from kimo_cli.config import Config
from kimo_cli.soul.agent import Runtime
from kimo_cli.tools.utils import ToolResultBuilder, load_desc
from kimo_cli.utils.aiohttp import new_client_session
from kimo_cli.utils.logging import logger

# URLs that are blocked to prevent SSRF attacks
_BLOCKED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "[::1]",
}


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _is_safe_url(url: str) -> bool:
    """Check if a URL is safe to fetch (not SSRF-prone).

    Kept as a standalone predicate (rather than delegating to
    ``_resolve_safe_addresses``) because the Playwright path has no connector to
    pin addresses onto; the two call sites stay independently patchable.
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if not host:
            return False
        # Block localhost and private IPs
        if host.lower() in _BLOCKED_HOSTS:
            return False
        try:
            ip = ipaddress.ip_address(host)
            if _is_blocked_ip(ip):
                return False
        except ValueError:
            # Not an IP, check DNS resolution
            try:
                addr = socket.getaddrinfo(host, parsed.port or 80)
                for _, _, _, _, sockaddr in addr:
                    if _is_blocked_ip(ipaddress.ip_address(sockaddr[0])):
                        return False
            except socket.gaierror:
                pass
        return True
    except Exception:
        return False


def _resolve_safe_addresses(url: str) -> tuple[str, list[str]] | None:
    """Validate a URL and return the exact addresses that may be connected to.

    Returning the addresses (instead of just a yes/no verdict) is what closes the
    DNS-rebinding window: the caller pins the connector to this list, so the name
    is never resolved a second time between the check and the connect.

    Returns:
        ``(hostname, addresses)`` if the URL is safe to fetch, ``None`` otherwise.
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if not host:
            return None
        if host.lower() in _BLOCKED_HOSTS:
            return None
        try:
            ip = ipaddress.ip_address(host)
            if _is_blocked_ip(ip):
                return None
            # An IP literal is connected to directly; aiohttp never resolves it.
            return host, [str(ip)]
        except ValueError:
            try:
                addr = socket.getaddrinfo(host, parsed.port or 80)
            except socket.gaierror:
                # Unresolvable names are left to the HTTP client, which reports a
                # clearer connection error than a blanket "blocked".
                return host, []
            addresses: list[str] = []
            for _, _, _, _, sockaddr in addr:
                resolved = ipaddress.ip_address(sockaddr[0])
                if _is_blocked_ip(resolved):
                    return None
                addresses.append(str(resolved))
            return host, addresses
    except Exception:
        return None


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """Resolver that only ever returns addresses validated before the request.

    Without it, ``_is_safe_url`` and the TLS connection each perform their own
    ``getaddrinfo``: an attacker-controlled name with a short TTL can answer
    "public IP" to the check and "127.0.0.1" to the connect.
    """

    def __init__(self, host: str, addresses: list[str]) -> None:
        self._host = host.lower()
        self._addresses = addresses

    @override
    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[aiohttp.abc.ResolveResult]:
        if host.lower() != self._host or not self._addresses:
            raise OSError(f"Refusing to resolve unexpected host: {host}")
        results: list[aiohttp.abc.ResolveResult] = []
        for address in self._addresses:
            ip = ipaddress.ip_address(address)
            if family == socket.AF_INET and ip.version != 4:
                continue
            if family == socket.AF_INET6 and ip.version != 6:
                continue
            results.append(
                {
                    "hostname": host,
                    "host": address,
                    "port": port,
                    "family": socket.AF_INET if ip.version == 4 else socket.AF_INET6,
                    "proto": 0,
                    "flags": 0,
                }
            )
        if not results:
            raise OSError(f"No address of the requested family for {host}: {self._addresses}")
        return results

    @override
    async def close(self) -> None:
        return None


class Params(BaseModel):
    url: str = Field(description="The URL to fetch content from.")


# Modern browser headers to reduce bot detection
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


class FetchURL(CallableTool2[Params]):
    name: str = "FetchURL"
    description: str = load_desc(Path(__file__).parent / "fetch.md", {})
    params: type[Params] = Params

    def __init__(self, config: Config, runtime: Runtime):
        super().__init__()

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        return await self.fetch_with_http_get(params)

    @staticmethod
    async def fetch_with_http_get(params: Params) -> ToolReturnValue:
        builder = ToolResultBuilder(max_line_length=None)
        # SSRF protection: block localhost, private IPs, and link-local addresses.
        # The resolved addresses are pinned onto the connector below so the name is
        # not resolved again at connect time (DNS rebinding).
        resolved = await asyncio.to_thread(_resolve_safe_addresses, params.url)
        if resolved is None:
            return builder.error(
                "URL blocked for security reasons (localhost/private IP)",
                brief="Blocked URL",
            )
        host, addresses = resolved
        resp_text: str | None = None
        try:
            # Fetching arbitrary web pages can take a while on large/slow sites.
            fetch_timeout = aiohttp.ClientTimeout(total=180, sock_read=60, sock_connect=15)
            async with (
                new_client_session(
                    timeout=fetch_timeout,
                    resolver=_PinnedResolver(host, addresses),
                ) as session,
                session.get(
                    params.url,
                    headers=_BROWSER_HEADERS.copy(),
                    allow_redirects=False,
                ) as response,
            ):
                if response.status >= 400:
                    logger.warning(
                        "FetchURL HTTP error: status={status}, url={url}",
                        status=response.status,
                        url=params.url,
                    )
                    # Fallback to Playwright for 403 (bot detection)
                    if response.status == 403:
                        logger.info("FetchURL falling back to Playwright for {url}", url=params.url)
                        return await FetchURL._fetch_with_playwright(params)
                    return builder.error(
                        (
                            f"Failed to fetch URL. Status: {response.status}. "
                            f"This may indicate the page is not accessible or the server is down."
                        ),
                        brief=f"HTTP {response.status} error",
                    )

                resp_text = await response.text()

                content_type = response.headers.get(aiohttp.hdrs.CONTENT_TYPE, "").lower()
                if content_type.startswith(("text/plain", "text/markdown")):
                    builder.write(resp_text)
                    return builder.ok("The returned content is the full content of the page.")
        except TimeoutError:
            logger.warning("FetchURL timed out: url={url}", url=params.url)
            return builder.error(
                "Failed to fetch URL: request timed out. The server may be slow or unreachable.",
                brief="Request timed out",
            )
        except aiohttp.ClientError as e:
            logger.warning("FetchURL network error: {error}, url={url}", error=e, url=params.url)
            return builder.error(
                (
                    f"Failed to fetch URL due to network error: {e}. "
                    "This may indicate the URL is invalid or the server is unreachable."
                ),
                brief="Network error",
            )

        if not resp_text:
            return builder.ok(
                "The response body is empty.",
                brief="Empty response body",
            )

        extracted_text = trafilatura.extract(
            resp_text,
            include_comments=True,
            include_tables=True,
            include_formatting=False,
            output_format="txt",
            with_metadata=True,
        )

        if not extracted_text:
            return builder.error(
                (
                    "Failed to extract meaningful content from the page. "
                    "This may indicate the page content is not suitable for text extraction, "
                    "or the page requires JavaScript to render its content."
                ),
                brief="No content extracted",
            )

        builder.write(extracted_text)
        return builder.ok("The returned content is the main text content extracted from the page.")

    @staticmethod
    async def _fetch_with_playwright(params: Params) -> ToolReturnValue:
        """Fallback fetch using Playwright + Chromium for bot-protected sites."""
        builder = ToolResultBuilder(max_line_length=None)
        if not await asyncio.to_thread(_is_safe_url, params.url):
            return builder.error(
                "URL blocked for security reasons (localhost/private IP)",
                brief="Blocked URL",
            )
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return builder.error(
                "Playwright is not available. Cannot fetch bot-protected pages.",
                brief="Playwright not installed",
            )

        try:
            async with async_playwright() as p:
                # try/finally around everything after launch: goto() has a 30s
                # timeout and content() can fail too, and the broad except
                # below caught those without ever closing the browser. Each
                # failed fetch left a Chromium subprocess behind until the
                # process exited.
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                try:
                    context = await browser.new_context(
                        user_agent=_BROWSER_HEADERS["User-Agent"],
                        viewport={"width": 1920, "height": 1080},
                    )
                    page = await context.new_page()
                    await page.goto(
                        params.url,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    # Wait a moment for any lazy-loaded content
                    await page.wait_for_timeout(2000)
                    content = await page.content()
                finally:
                    await browser.close()
        except Exception as e:
            logger.warning(
                "FetchURL Playwright fallback failed: {error}, url={url}",
                error=e,
                url=params.url,
            )
            message = str(e)
            if "Executable doesn't exist" in message:
                # The browser binary playwright wants is not where it is
                # looking. The env var names the install the launcher set up
                # (or <unset> when the bundled browsers directory went
                # missing) — without it, self-diagnosing this on a user's
                # machine is guesswork.
                browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "<unset>")
                return builder.error(
                    (
                        "Browser fallback unavailable: the Playwright browser binary is missing. "
                        f"PLAYWRIGHT_BROWSERS_PATH={browsers_path}; "
                        f"playwright looked for: {message}. "
                        "Causes, in order of likelihood: the install is missing "
                        "runtime/playwright-browsers (repair/reinstall the app), the app was "
                        "running before that directory existed (restart the app), or the bundled "
                        "chromium does not match the installed playwright version (version "
                        "mismatch after a pip install in the user overlay)."
                    ),
                    brief="Playwright browser binary missing",
                )
            return builder.error(
                f"Failed to fetch URL via browser fallback: {e}",
                brief="Browser fetch failed",
            )

        if not content:
            return builder.ok(
                "The page content is empty.",
                brief="Empty page content",
            )

        extracted_text = trafilatura.extract(
            content,
            include_comments=True,
            include_tables=True,
            include_formatting=False,
            output_format="txt",
            with_metadata=True,
        )

        if not extracted_text:
            return builder.error(
                (
                    "Failed to extract meaningful content from the page. "
                    "The page may require interaction or specific browser features."
                ),
                brief="No content extracted via browser",
            )

        builder.write(extracted_text)
        return builder.ok("The returned content was fetched via browser automation.")
