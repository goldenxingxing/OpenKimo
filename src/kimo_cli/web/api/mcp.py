"""Admin API endpoints for managing MCP servers.

Edits the same ``~/.kimo/mcp.json`` the ``kimo mcp`` commands write, so the two
surfaces stay interchangeable. Every route is admin-only: the file holds the
bearer tokens and API keys MCP servers are reached with, and a stdio entry is a
command line this host will execute.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from kimo_cli import logger
from kimo_cli.mcp_probe import ProbeResult, probe_mcp_server
from kimo_cli.mcp_store import (
    MCPConfigFileError,
    get_global_mcp_config_file,
    load_mcp_document,
    save_mcp_document,
    save_mcp_text,
    server_transport,
    validate_mcp_document,
)
from kimo_cli.web.runner.process import KimoCLIRunner
from kimo_cli.web.user_auth import require_admin

router = APIRouter(prefix="/api/admin/mcp", tags=["admin", "mcp"])

# Lock to serialize all mutations of mcp.json — prevents TOCTOU races where
# two concurrent requests both load, modify, and save the document, with the
# second save overwriting the first save's changes.
_mutation_lock = asyncio.Lock()

#: Opt in or out of editing stdio servers from the web UI, overriding the
#: default described in :func:`_stdio_editing_allowed`.
ENV_ALLOW_STDIO_MCP = "KIMO_WEB_ALLOW_STDIO_MCP"

#: Marks a secret the API replaced with a masked stand-in. A value arriving
#: back with this prefix means "keep what is on disk" — see :func:`_unmask`.
_MASK_PREFIX = "***"

#: Server names go into a URL path and a filesystem-ish config key; keep them
#: boring rather than discovering later which characters break something.
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class MCPServerEntry(BaseModel):
    """One configured server, with its secrets masked."""

    name: str
    transport: str = Field(description="stdio, http or sse")
    target: str = Field(description="Command line or URL, for display")
    config: dict[str, Any] = Field(description="The mcp.json entry, secrets masked")
    uses_oauth: bool = Field(description="Whether the entry authorizes via OAuth")
    oauth_authorized: bool | None = Field(
        default=None,
        description="Whether cached OAuth tokens exist; null when the entry does not use OAuth",
    )


class MCPServerList(BaseModel):
    path: str = Field(description="Absolute path of mcp.json")
    servers: list[MCPServerEntry]
    stdio_editable: bool = Field(
        description="Whether this deployment allows adding or editing stdio servers"
    )


class CreateMCPServerRequest(BaseModel):
    name: str
    config: dict[str, Any]


class UpdateMCPServerRequest(BaseModel):
    config: dict[str, Any]


class RawMCPConfig(BaseModel):
    path: str
    content: str


class WriteRawMCPConfigRequest(BaseModel):
    content: str


class MCPMutationResponse(BaseModel):
    """What the config looks like now, and which sessions picked the change up."""

    servers: MCPServerList
    restarted_session_ids: list[str] | None = None
    skipped_busy_session_ids: list[str] | None = None


class MCPProbeTool(BaseModel):
    name: str
    description: str


class MCPProbeResponse(BaseModel):
    ok: bool
    tools: list[MCPProbeTool] = []
    error: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_runner(req: Request) -> KimoCLIRunner:
    """Get KimoCLIRunner from FastAPI app state."""
    return req.app.state.runner


def _ensure_sensitive_apis_allowed(request: Request) -> None:
    """Block MCP config access when sensitive APIs are restricted.

    Same gate config.toml editing uses, for the same reason: this file holds
    credentials in plaintext.
    """
    if getattr(request.app.state, "restrict_sensitive_apis", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sensitive config APIs are disabled in this mode.",
        )


def _stdio_editing_allowed(request: Request) -> bool:
    """Whether this deployment lets the web UI configure stdio servers.

    A stdio entry is a command this host runs, so writing one through the
    browser is arbitrary code execution by whoever holds an admin account —
    a different proposition on a shared server than on someone's own laptop.
    The desktop app, which is that laptop, allows it; anything else has to say
    so explicitly via ``KIMO_WEB_ALLOW_STDIO_MCP``. Restricted deployments
    never allow it, whatever the variable says.
    """
    if getattr(request.app.state, "restrict_sensitive_apis", False):
        return False
    override = os.environ.get(ENV_ALLOW_STDIO_MCP)
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    # Set only by the packaging app, so it is the marker for "this is the
    # desktop build, talking to its own machine".
    return bool(os.environ.get("OPENKIMO_APP_DATA_DIR"))


def _ensure_stdio_allowed(request: Request, server_config: dict[str, Any]) -> None:
    if "command" not in server_config:
        return
    if _stdio_editing_allowed(request):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "Configuring stdio MCP servers from the web UI is disabled in this deployment, "
            f"because they run commands on the server. Set {ENV_ALLOW_STDIO_MCP}=1 to allow "
            "it, or use `kimo mcp add` on the host."
        ),
    )


def _stdio_entries(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The stdio servers in a document, as fastmcp will actually read it.

    Not ``document["mcpServers"]``: without that key fastmcp treats every
    top-level object with a ``command`` or ``url`` as a server, so a check that
    only looked under ``mcpServers`` would wave such a document through and a
    session would still launch its commands.
    """
    from fastmcp.mcp_config import MCPConfig, StdioMCPServer

    config = MCPConfig.model_validate(document)
    return {
        name: server.model_dump(mode="json")
        for name, server in config.mcpServers.items()
        if isinstance(server, StdioMCPServer)
    }


def _is_secret_key(key: str) -> bool:
    """Which top-level keys of a server entry hold credentials."""
    return key in {"headers", "env"}


def _mask_secret(value: Any) -> str:
    """Replace a credential with a hint of itself."""
    text = str(value)
    tail = text[-4:] if len(text) > 8 else ""
    return f"{_MASK_PREFIX}{tail}"


def _mask_config(server_config: dict[str, Any]) -> dict[str, Any]:
    """Copy an entry with every credential in it masked.

    Header and environment values are the tokens; ``auth`` is a bearer string
    unless it is the literal ``"oauth"``, which is a mode, not a secret.
    """
    masked: dict[str, Any] = {}
    for key, value in server_config.items():
        if _is_secret_key(key) and isinstance(value, dict):
            secrets = cast("dict[str, Any]", value)
            masked[key] = {k: _mask_secret(v) for k, v in secrets.items()}
        elif key == "auth" and isinstance(value, str) and value != "oauth":
            masked[key] = _mask_secret(value)
        else:
            masked[key] = value
    return masked


def _is_masked(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_MASK_PREFIX)


def _unmask(new_config: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
    """Restore the credentials a client sent back masked.

    The UI never receives the real values, so it cannot echo them. Anything
    still wearing a mask means "unchanged"; with nothing on disk to restore it
    from, that is a client bug worth a 400 rather than a server that silently
    stores the string ``***f3a2`` as an API key.
    """
    resolved: dict[str, Any] = {}
    for key, value in new_config.items():
        old_value = (existing or {}).get(key)
        if _is_secret_key(key) and isinstance(value, dict):
            old_map = cast("dict[str, Any]", old_value) if isinstance(old_value, dict) else {}
            pairs: dict[str, Any] = {}
            for k, v in cast("dict[str, Any]", value).items():
                if _is_masked(v):
                    if k not in old_map:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Masked value for '{key}.{k}' has nothing to restore from.",
                        )
                    pairs[k] = old_map[k]
                else:
                    pairs[k] = v
            resolved[key] = pairs
        elif key == "auth" and _is_masked(value):
            if not isinstance(old_value, str):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Masked value for 'auth' has nothing to restore from.",
                )
            resolved[key] = old_value
        else:
            resolved[key] = value
    return resolved


def _display_target(server_config: dict[str, Any]) -> str:
    if "command" in server_config:
        args = cast("list[Any]", server_config.get("args") or [])
        parts = [str(server_config["command"]), *(str(a) for a in args)]
        return " ".join(parts)
    return str(server_config.get("url", ""))


async def _entry(name: str, server_config: dict[str, Any]) -> MCPServerEntry:
    uses_oauth = server_config.get("auth") == "oauth"
    authorized: bool | None = None
    if uses_oauth:
        from kimo_cli.mcp_oauth import has_mcp_oauth_tokens

        url = server_config.get("url")
        authorized = await has_mcp_oauth_tokens(str(url)) if url else False
    return MCPServerEntry(
        name=name,
        transport=server_transport(server_config),
        target=_display_target(server_config),
        config=_mask_config(server_config),
        uses_oauth=uses_oauth,
        oauth_authorized=authorized,
    )


def _load_document() -> dict[str, Any]:
    try:
        return load_mcp_document()
    except MCPConfigFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


def _servers_of(document: dict[str, Any]) -> dict[str, Any]:
    servers = document.get("mcpServers")
    return cast("dict[str, Any]", servers) if isinstance(servers, dict) else {}


async def _server_list(request: Request, document: dict[str, Any]) -> MCPServerList:
    servers = _servers_of(document)
    entries = [await _entry(name, cfg) for name, cfg in servers.items()]
    return MCPServerList(
        path=str(get_global_mcp_config_file()),
        servers=entries,
        stdio_editable=_stdio_editing_allowed(request),
    )


def _validate_entry(name: str, server_config: dict[str, Any]) -> None:
    """Check one entry on its own, so the error names the server being edited."""
    if not _NAME_PATTERN.match(name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid server name '{name}'. Use letters, digits, '.', '-' and '_', "
                "starting with a letter or digit."
            ),
        )
    try:
        validate_mcp_document({"mcpServers": {name: server_config}})
    except MCPConfigFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


def _save(document: dict[str, Any], raw_content: str | None) -> None:
    try:
        if raw_content is None:
            save_mcp_document(document)
        else:
            # Hand-edited text goes back verbatim: re-serializing it would
            # reformat the file under someone who chose how it should look.
            save_mcp_text(raw_content)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to write {get_global_mcp_config_file()}: {exc}",
        ) from exc


async def _respond_after_save(
    request: Request,
    document: dict[str, Any],
    runner: KimoCLIRunner,
) -> MCPMutationResponse:
    """Restart the sessions that can take a change already persisted to disk.

    MCP servers are connected when a worker starts, so an edit reaches a live
    session only through a restart. Busy sessions are left alone and named in
    the response — interrupting a running turn to pick up a config change is
    the user's call, not ours. The caller must have finished the save while
    holding ``_mutation_lock``; the restart itself must not hold that lock, or
    every other MCP edit is serialized behind a worker restart.
    """
    restarted: list[str] = []
    skipped_busy: list[str] = []
    try:
        summary = await runner.restart_running_workers(reason="mcp_update", force=False)
        restarted = [str(sid) for sid in summary.restarted_session_ids]
        skipped_busy = [str(sid) for sid in summary.skipped_busy_session_ids]
    except Exception as e:
        logger.warning(f"Failed to restart workers after MCP config update: {e}")

    return MCPMutationResponse(
        servers=await _server_list(request, document),
        restarted_session_ids=restarted or None,
        skipped_busy_session_ids=skipped_busy or None,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/servers", summary="List configured MCP servers (admin only)")
async def list_mcp_servers(
    http_request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> MCPServerList:
    """List the servers in the global mcp.json, with credentials masked."""
    _ensure_sensitive_apis_allowed(http_request)
    return await _server_list(http_request, _load_document())


@router.post("/servers", summary="Add an MCP server (admin only)")
async def create_mcp_server(
    body: CreateMCPServerRequest,
    http_request: Request,
    runner: KimoCLIRunner = Depends(_get_runner),
    admin: dict[str, Any] = Depends(require_admin),
) -> MCPMutationResponse:
    """Add a server to the global mcp.json."""
    _ensure_sensitive_apis_allowed(http_request)
    _ensure_stdio_allowed(http_request, body.config)

    async with _mutation_lock:
        document = _load_document()
        servers = _servers_of(document)
        if body.name in servers:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"MCP server '{body.name}' already exists.",
            )
        config = _unmask(body.config, None)
        _validate_entry(body.name, config)

        servers[body.name] = config
        document["mcpServers"] = servers
        _save(document, None)
    return await _respond_after_save(http_request, document, runner)


@router.put("/servers/{name}", summary="Update an MCP server (admin only)")
async def update_mcp_server(
    name: str,
    body: UpdateMCPServerRequest,
    http_request: Request,
    runner: KimoCLIRunner = Depends(_get_runner),
    admin: dict[str, Any] = Depends(require_admin),
) -> MCPMutationResponse:
    """Replace one server's entry in the global mcp.json."""
    _ensure_sensitive_apis_allowed(http_request)

    async with _mutation_lock:
        document = _load_document()
        servers = _servers_of(document)
        existing = servers.get(name)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"MCP server '{name}' not found.",
            )
        # Both sides are checked: an existing stdio entry must not be editable
        # where stdio is disallowed, and no entry may be turned into one.
        existing_config = cast("dict[str, Any]", existing) if isinstance(existing, dict) else None
        _ensure_stdio_allowed(http_request, existing_config or {})
        _ensure_stdio_allowed(http_request, body.config)

        config = _unmask(body.config, existing_config)
        _validate_entry(name, config)

        servers[name] = config
        document["mcpServers"] = servers
        _save(document, None)
    return await _respond_after_save(http_request, document, runner)


@router.delete("/servers/{name}", summary="Remove an MCP server (admin only)")
async def delete_mcp_server(
    name: str,
    http_request: Request,
    runner: KimoCLIRunner = Depends(_get_runner),
    admin: dict[str, Any] = Depends(require_admin),
) -> MCPMutationResponse:
    """Remove a server from the global mcp.json."""
    _ensure_sensitive_apis_allowed(http_request)

    async with _mutation_lock:
        document = _load_document()
        servers = _servers_of(document)
        if name not in servers:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"MCP server '{name}' not found.",
            )
        del servers[name]
        document["mcpServers"] = servers
        _save(document, None)
    return await _respond_after_save(http_request, document, runner)


@router.post("/servers/{name}/test", summary="Test an MCP server connection (admin only)")
async def test_mcp_server(
    name: str,
    http_request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> MCPProbeResponse:
    """Connect to one configured server and list the tools it offers."""
    _ensure_sensitive_apis_allowed(http_request)

    servers = _servers_of(_load_document())
    server_config = servers.get(name)
    if not isinstance(server_config, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MCP server '{name}' not found.",
        )

    config = cast("dict[str, Any]", server_config)
    if config.get("auth") == "oauth":
        from kimo_cli.mcp_oauth import has_mcp_oauth_tokens

        url = config.get("url")
        if not url or not await has_mcp_oauth_tokens(str(url)):
            # Probing would start the interactive OAuth login — a browser and a
            # callback listener on the server host, not in front of this user.
            return MCPProbeResponse(
                ok=False,
                error=f"Not authorized yet. Run `kimo mcp auth {name}` on the host first.",
            )

    result: ProbeResult = await probe_mcp_server(name, config)
    return MCPProbeResponse(
        ok=result.ok,
        tools=[MCPProbeTool(name=t.name, description=t.description) for t in result.tools],
        error=result.error,
    )


@router.get("/raw", summary="Read the raw mcp.json (admin only)")
async def get_raw_mcp_config(
    http_request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> RawMCPConfig:
    """Return mcp.json verbatim — credentials included, for hand editing."""
    _ensure_sensitive_apis_allowed(http_request)
    path = get_global_mcp_config_file()
    if not path.is_file():
        return RawMCPConfig(path=str(path), content="")
    try:
        return RawMCPConfig(path=str(path), content=path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read {path}: {exc}",
        ) from exc


@router.put("/raw", summary="Replace the raw mcp.json (admin only)")
async def put_raw_mcp_config(
    body: WriteRawMCPConfigRequest,
    http_request: Request,
    runner: KimoCLIRunner = Depends(_get_runner),
    admin: dict[str, Any] = Depends(require_admin),
) -> MCPMutationResponse:
    """Replace the whole mcp.json, after checking it parses and validates."""
    import json

    _ensure_sensitive_apis_allowed(http_request)

    async with _mutation_lock:
        blank = not body.content.strip()
        try:
            parsed: Any = {"mcpServers": {}} if blank else json.loads(body.content)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid JSON: {exc}",
            ) from exc
        if not isinstance(parsed, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="MCP config must be a JSON object.",
            )
        document = cast("dict[str, Any]", parsed)
        try:
            validate_mcp_document(document)
        except MCPConfigFileError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        # The raw editor can introduce a stdio server just as the form can. Only a
        # new or changed one needs the gate: a stdio entry added on the host with
        # `kimo mcp add` and left as it was must not make every raw save a 403.
        if not _stdio_editing_allowed(http_request):
            try:
                on_disk = _stdio_entries(load_mcp_document())
            except MCPConfigFileError:
                on_disk = {}
            for name, entry in _stdio_entries(document).items():
                if on_disk.get(name) != entry:
                    _ensure_stdio_allowed(http_request, {"command": entry["command"]})

        # Blank text would be written as a file that is not JSON, which the CLI
        # and every session then refuse to read; store the empty document instead.
        _save(document, None if blank else body.content)
    return await _respond_after_save(http_request, document, runner)


__all__ = ["router"]
