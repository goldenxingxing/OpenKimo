"""Connect to one MCP server and report what it offers.

The same question — "is this server reachable, and which tools does it expose?"
— is asked by ``kimo mcp test`` and by the admin panel's Test button, so both
go through :func:`probe_mcp_server`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

#: How long a probe waits before calling the server unreachable. The agent's
#: own connect path has no timeout at all, which is survivable there because it
#: runs in the background; a probe is answering an HTTP request, so it cannot
#: hang on a stdio server that never speaks.
DEFAULT_PROBE_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class ProbeTool:
    """One tool advertised by a probed server."""

    name: str
    description: str


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of a probe.

    Attributes:
        ok: Whether the server was reached and listed its tools.
        tools: The tools it advertised; empty when ``ok`` is False.
        error: ``TypeName: message`` when it failed, otherwise None.
    """

    ok: bool
    tools: list[ProbeTool] = field(default_factory=lambda: [])
    error: str | None = None


async def probe_mcp_server(
    name: str,
    server_config: dict[str, Any],
    *,
    timeout_s: float | None = DEFAULT_PROBE_TIMEOUT_S,
) -> ProbeResult:
    """Connect to a single MCP server and list its tools.

    Never raises for a server-side problem: a failure to connect is an answer,
    not an error. Only cancellation propagates.

    Args:
        name: The server's name in the config; only used to build the client.
        server_config: Its entry from ``mcp.json``.
        timeout_s: Ceiling on the whole connect-and-list round trip; None for
            none, when the connect includes an interactive OAuth login.

    Returns:
        What the server said, or why it could not be asked.
    """
    import fastmcp

    from kimo_cli.mcp_oauth import prepare_mcp_server_config

    try:
        prepared = prepare_mcp_server_config(server_config)
        client = fastmcp.Client({"mcpServers": {name: prepared}})
        async with asyncio.timeout(timeout_s):
            async with client:
                tools: list[Any] = await client.list_tools()
    except TimeoutError:
        # timeout_s can be None for interactive OAuth logins. A TimeoutError
        # can still come from the client, so format defensively.
        limit = f"within {timeout_s:g}s" if timeout_s is not None else "without a timeout"
        return ProbeResult(ok=False, error=f"TimeoutError: no response {limit}")
    except Exception as e:
        return ProbeResult(ok=False, error=f"{type(e).__name__}: {e}")

    return ProbeResult(
        ok=True,
        tools=[ProbeTool(name=t.name, description=t.description or "") for t in tools],
    )


__all__ = ["DEFAULT_PROBE_TIMEOUT_S", "ProbeResult", "ProbeTool", "probe_mcp_server"]
