"""The shared MCP config store and the connection probe.

Both are used by two callers each — ``kimo mcp`` and the admin API — so the
behaviour pinned here is what keeps those two surfaces telling the same story.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kimo_cli import mcp_store
from kimo_cli.mcp_probe import DEFAULT_PROBE_TIMEOUT_S, probe_mcp_server
from kimo_cli.mcp_store import (
    MCPConfigFileError,
    load_mcp_document,
    load_mcp_servers,
    save_mcp_document,
    save_mcp_text,
    server_transport,
)


@pytest.fixture(autouse=True)
def _share_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path / "share"))


def test_missing_file_reads_as_empty() -> None:
    assert load_mcp_document() == {"mcpServers": {}}
    assert load_mcp_servers() == {}


def test_round_trip(tmp_path: Path) -> None:
    save_mcp_document({"mcpServers": {"a": {"url": "https://a.example/mcp"}}})

    assert load_mcp_servers() == {"a": {"url": "https://a.example/mcp"}}
    # Written where the CLI looks for it, and readable as plain JSON.
    written = (tmp_path / "share" / "mcp.json").read_text(encoding="utf-8")
    assert json.loads(written)["mcpServers"]["a"]["url"] == "https://a.example/mcp"
    assert written.endswith("\n")


def test_save_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    save_mcp_text('{"mcpServers": {}}')

    assert [p.name for p in (tmp_path / "share").iterdir()] == ["mcp.json"]


def test_a_failed_write_leaves_the_old_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_mcp_document({"mcpServers": {"good": {"url": "https://a.example/mcp"}}})

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mcp_store.os, "replace", explode)
    with pytest.raises(OSError):
        save_mcp_document({"mcpServers": {"other": {"url": "https://b.example/mcp"}}})

    assert load_mcp_servers() == {"good": {"url": "https://a.example/mcp"}}


def test_broken_json_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "share" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(MCPConfigFileError) as excinfo:
        load_mcp_document()

    assert "mcp.json" in str(excinfo.value)


def test_invalid_config_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "share" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"a": {"description": "no target"}}}), "utf-8")

    with pytest.raises(MCPConfigFileError):
        load_mcp_document()


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"command": "npx", "args": ["x"]}, "stdio"),
        ({"url": "https://a.example/mcp"}, "http"),
        ({"url": "https://a.example/mcp", "transport": "streamable-http"}, "http"),
        ({"url": "https://a.example/sse"}, "sse"),
        ({"url": "https://a.example/sse/"}, "sse"),
        ({"url": "https://a.example/mcp", "transport": "sse"}, "sse"),
    ],
)
def test_transport_inference(config: dict[str, Any], expected: str) -> None:
    assert server_transport(config) == expected


class _FakeTool:
    def __init__(self, name: str, description: str | None) -> None:
        self.name = name
        self.description = description


class _FakeClient:
    def __init__(self, tools: list[_FakeTool] | None = None, error: Exception | None = None):
        self._tools = tools or []
        self._error = error

    async def __aenter__(self) -> _FakeClient:
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def list_tools(self) -> list[_FakeTool]:
        return self._tools


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    import fastmcp

    monkeypatch.setattr(fastmcp, "Client", lambda *_a, **_kw: client)


async def test_probe_reports_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, _FakeClient([_FakeTool("search", None)]))

    result = await probe_mcp_server("a", {"url": "https://a.example/mcp"})

    assert result.ok
    assert result.error is None
    # A tool without a description reads as empty, not None, so callers can
    # print it without a special case.
    assert [(t.name, t.description) for t in result.tools] == [("search", "")]


async def test_probe_turns_a_failure_into_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, _FakeClient(error=ConnectionError("refused")))

    result = await probe_mcp_server("a", {"url": "https://a.example/mcp"})

    assert not result.ok
    assert result.tools == []
    assert result.error == "ConnectionError: refused"


async def test_probe_gives_up_on_a_server_that_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    class _Hang(_FakeClient):
        async def __aenter__(self) -> _FakeClient:
            await asyncio.sleep(3600)
            return self

    _patch_client(monkeypatch, _Hang())

    result = await probe_mcp_server("a", {"url": "https://a.example/mcp"}, timeout_s=0.05)

    assert not result.ok
    assert result.error is not None and result.error.startswith("TimeoutError")


def test_probe_timeout_has_a_sane_default() -> None:
    # Long enough for a cold stdio server to boot, short enough that an HTTP
    # request waiting on it still returns.
    assert 5 <= DEFAULT_PROBE_TIMEOUT_S <= 60
