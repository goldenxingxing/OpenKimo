from __future__ import annotations

import ssl

import aiohttp
import certifi

_ssl_context = ssl.create_default_context(cafile=certifi.where())

_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(
    total=120,
    sock_read=60,
    sock_connect=15,
)


def new_client_session(
    *,
    timeout: aiohttp.ClientTimeout | None = None,
    resolver: aiohttp.abc.AbstractResolver | None = None,
) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(ssl=_ssl_context, resolver=resolver)
    return aiohttp.ClientSession(
        connector=connector,
        timeout=timeout or _DEFAULT_TIMEOUT,
    )
