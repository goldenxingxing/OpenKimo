"""Read and write the global MCP server config (``~/.kimo/mcp.json``).

The file is the Claude-Desktop-compatible ``{"mcpServers": {...}}`` document
that :mod:`fastmcp` validates; kimo does not define a schema of its own. This
module owns the file so that the CLI (``kimo mcp ...``) and the web admin API
agree on where it lives, what counts as valid, and how it is written.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, cast

from kimo_cli.share import get_share_dir


class MCPConfigFileError(Exception):
    """The MCP config file could not be read, parsed or validated."""


def get_global_mcp_config_file() -> Path:
    """Get the global MCP config file path."""
    return get_share_dir() / "mcp.json"


def load_mcp_document() -> dict[str, Any]:
    """Read and validate the whole ``mcp.json`` document.

    Returns:
        The parsed document, or an empty one when the file does not exist yet.

    Raises:
        MCPConfigFileError: The file is not JSON, or not a valid MCP config.
    """
    mcp_file = get_global_mcp_config_file()
    if not mcp_file.exists():
        return {"mcpServers": {}}
    try:
        document = json.loads(mcp_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise MCPConfigFileError(f"Invalid JSON in MCP config file '{mcp_file}': {e}") from e
    except UnicodeDecodeError as e:
        raise MCPConfigFileError(f"Non-UTF-8 content in MCP config file '{mcp_file}': {e}") from e
    except OSError as e:
        raise MCPConfigFileError(f"Failed to read MCP config file '{mcp_file}': {e}") from e

    validate_mcp_document(document, source=str(mcp_file))
    return document


def validate_mcp_document(document: Any, *, source: str | None = None) -> None:
    """Validate a document against fastmcp's MCP config model.

    Args:
        document: The parsed ``mcpServers`` document.
        source: Where it came from, for the error message; the file path when
            validating what is on disk, omitted for an unsaved edit.

    Raises:
        MCPConfigFileError: The document is not a valid MCP config.
    """
    from fastmcp.mcp_config import MCPConfig
    from pydantic import ValidationError

    try:
        MCPConfig.model_validate(document)
    except ValidationError as e:
        where = f" in '{source}'" if source else ""
        raise MCPConfigFileError(f"Invalid MCP config{where}: {e}") from e


def load_mcp_servers() -> dict[str, Any]:
    """The ``mcpServers`` mapping of the global config, keyed by server name."""
    servers = load_mcp_document().get("mcpServers", {})
    return cast("dict[str, Any]", servers) if isinstance(servers, dict) else {}


def save_mcp_document(document: dict[str, Any]) -> None:
    """Serialize and write the whole document out, atomically."""
    save_mcp_text(json.dumps(document, indent=2, ensure_ascii=False))


def save_mcp_text(content: str) -> None:
    """Write the config file's exact text, atomically.

    A plain truncate-and-write leaves a half-written ``mcp.json`` behind if the
    process dies mid-write, and every session that starts afterwards then comes
    up with no MCP servers at all. Written to a temporary file in the same
    directory and moved into place instead, so a reader sees either the old
    document or the new one.
    """
    mcp_file = get_global_mcp_config_file()
    mcp_file.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".mcp-", suffix=".json", dir=mcp_file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            if not content.endswith("\n"):
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, mcp_file)
        if os.name != "nt":
            with contextlib.suppress(OSError):
                dir_fd = os.open(mcp_file.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
    finally:
        # Gone already on the success path, where os.replace moved it.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def server_transport(server_config: dict[str, Any]) -> str:
    """Name the transport a server entry uses: ``stdio``, ``http`` or ``sse``.

    Mirrors fastmcp's own inference: an entry with a command is stdio, and a
    remote entry without an explicit transport is http unless its URL ends in
    ``/sse``.
    """
    if "command" in server_config:
        return "stdio"
    transport = server_config.get("transport")
    if transport in {"http", "streamable-http"}:
        return "http"
    if transport == "sse":
        return "sse"
    url = str(server_config.get("url", ""))
    return "sse" if url.rstrip("/").endswith("/sse") else "http"


__all__ = [
    "MCPConfigFileError",
    "get_global_mcp_config_file",
    "load_mcp_document",
    "load_mcp_servers",
    "save_mcp_document",
    "save_mcp_text",
    "server_transport",
    "validate_mcp_document",
]
