"""Admin MCP config API.

Pins the properties that are easy to lose in a refactor and expensive to lose
in production: credentials never leave the process in the clear, a masked value
coming back never overwrites the real one, stdio servers stay behind the
deployment gate that makes web-configured command execution a deliberate
choice, and a saved edit asks live sessions to pick it up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kimo_cli.web.api import mcp
from kimo_cli.web.user_auth import require_admin


class _RestartSummary:
    def __init__(self, restarted: list[str], skipped: list[str]) -> None:
        self.restarted_session_ids = restarted
        self.skipped_busy_session_ids = skipped


class _FakeRunner:
    """Records restart requests; a real runner would need a whole app."""

    def __init__(self, restarted: list[str] | None = None, skipped: list[str] | None = None):
        self.calls: list[tuple[str, bool]] = []
        self._summary = _RestartSummary(restarted or [], skipped or [])

    async def restart_running_workers(self, *, reason: str, force: bool) -> _RestartSummary:
        self.calls.append((reason, force))
        return self._summary


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner: _FakeRunner | None = None,
    allow_stdio: bool | None = None,
    restrict_sensitive: bool = False,
) -> tuple[TestClient, _FakeRunner]:
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path / "share"))
    monkeypatch.delenv("OPENKIMO_APP_DATA_DIR", raising=False)
    if allow_stdio is None:
        monkeypatch.delenv(mcp.ENV_ALLOW_STDIO_MCP, raising=False)
    else:
        monkeypatch.setenv(mcp.ENV_ALLOW_STDIO_MCP, "1" if allow_stdio else "0")

    app = FastAPI()
    app.include_router(mcp.router)
    app.dependency_overrides[require_admin] = lambda: {"id": "admin", "role": "admin"}
    fake_runner = runner or _FakeRunner()
    app.state.runner = fake_runner
    app.state.restrict_sensitive_apis = restrict_sensitive
    return TestClient(app), fake_runner


def _write_config(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "share" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _stored(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "share" / "mcp.json").read_text(encoding="utf-8"))


def test_list_masks_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(
        tmp_path,
        {
            "mcpServers": {
                "context7": {
                    "url": "https://mcp.context7.com/mcp",
                    "transport": "http",
                    "headers": {"CONTEXT7_API_KEY": "ctx7sk-super-secret-value"},
                }
            }
        },
    )
    client, _ = _client(tmp_path, monkeypatch)

    body = client.get("/api/admin/mcp/servers").json()

    assert body["servers"][0]["name"] == "context7"
    assert body["servers"][0]["transport"] == "http"
    header = body["servers"][0]["config"]["headers"]["CONTEXT7_API_KEY"]
    assert header.startswith("***")
    assert "super-secret" not in json.dumps(body)


def test_masked_value_keeps_the_stored_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(
        tmp_path,
        {
            "mcpServers": {
                "context7": {
                    "url": "https://mcp.context7.com/mcp",
                    "transport": "http",
                    "headers": {"CONTEXT7_API_KEY": "ctx7sk-super-secret-value"},
                }
            }
        },
    )
    client, _ = _client(tmp_path, monkeypatch)

    # The UI only ever saw the mask, so this is what a rename of the URL sends back.
    response = client.put(
        "/api/admin/mcp/servers/context7",
        json={
            "config": {
                "url": "https://mcp.context7.com/v2/mcp",
                "transport": "http",
                "headers": {"CONTEXT7_API_KEY": "***alue"},
            }
        },
    )

    assert response.status_code == 200
    stored = _stored(tmp_path)["mcpServers"]["context7"]
    assert stored["headers"]["CONTEXT7_API_KEY"] == "ctx7sk-super-secret-value"
    assert stored["url"] == "https://mcp.context7.com/v2/mcp"


def test_masked_value_with_nothing_to_restore_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/admin/mcp/servers",
        json={
            "name": "fresh",
            "config": {
                "url": "https://example.com/mcp",
                "transport": "http",
                "headers": {"AUTHORIZATION": "***f3a2"},
            },
        },
    )

    assert response.status_code == 400
    assert not (tmp_path / "share" / "mcp.json").exists()


def test_create_and_delete_restart_idle_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _FakeRunner(restarted=["s-1"], skipped=["s-2"])
    client, fake = _client(tmp_path, monkeypatch, runner=runner)

    created = client.post(
        "/api/admin/mcp/servers",
        json={
            "name": "context7",
            "config": {"url": "https://mcp.context7.com/mcp", "transport": "http"},
        },
    )

    assert created.status_code == 200
    assert created.json()["restarted_session_ids"] == ["s-1"]
    assert created.json()["skipped_busy_session_ids"] == ["s-2"]
    assert fake.calls == [("mcp_update", False)]
    assert list(_stored(tmp_path)["mcpServers"]) == ["context7"]

    removed = client.delete("/api/admin/mcp/servers/context7")
    assert removed.status_code == 200
    assert _stored(tmp_path)["mcpServers"] == {}


def test_duplicate_and_missing_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, {"mcpServers": {"a": {"url": "https://a.example/mcp"}}})
    client, _ = _client(tmp_path, monkeypatch)

    duplicate = client.post(
        "/api/admin/mcp/servers",
        json={"name": "a", "config": {"url": "https://b.example/mcp"}},
    )
    assert duplicate.status_code == 409

    missing = client.put(
        "/api/admin/mcp/servers/nope",
        json={"config": {"url": "https://b.example/mcp"}},
    )
    assert missing.status_code == 404
    assert client.delete("/api/admin/mcp/servers/nope").status_code == 404


def test_invalid_entry_is_rejected_before_the_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, {"mcpServers": {"a": {"url": "https://a.example/mcp"}}})
    client, fake = _client(tmp_path, monkeypatch)

    # Neither a command nor a URL: not a server fastmcp can build.
    response = client.post(
        "/api/admin/mcp/servers",
        json={"name": "broken", "config": {"description": "nothing to connect to"}},
    )

    assert response.status_code == 400
    assert list(_stored(tmp_path)["mcpServers"]) == ["a"]
    assert fake.calls == []


def test_server_name_must_be_boring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/admin/mcp/servers",
        json={"name": "../escape", "config": {"url": "https://a.example/mcp"}},
    )

    assert response.status_code in {400, 404}


def test_stdio_is_refused_unless_the_deployment_allows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fake = _client(tmp_path, monkeypatch, allow_stdio=False)

    listed = client.get("/api/admin/mcp/servers").json()
    assert listed["stdio_editable"] is False

    response = client.post(
        "/api/admin/mcp/servers",
        json={"name": "local", "config": {"command": "npx", "args": ["evil-mcp"]}},
    )

    assert response.status_code == 403
    assert fake.calls == []

    # The raw editor is the same door.
    raw = client.put(
        "/api/admin/mcp/raw",
        json={"content": json.dumps({"mcpServers": {"local": {"command": "npx"}}})},
    )
    assert raw.status_code == 403


def test_stdio_is_allowed_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(tmp_path, monkeypatch, allow_stdio=True)

    response = client.post(
        "/api/admin/mcp/servers",
        json={"name": "local", "config": {"command": "npx", "args": ["some-mcp"]}},
    )

    assert response.status_code == 200
    assert _stored(tmp_path)["mcpServers"]["local"]["command"] == "npx"
    assert response.json()["servers"]["servers"][0]["transport"] == "stdio"


def test_desktop_app_allows_stdio_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENKIMO_APP_DATA_DIR", str(tmp_path / "app-data"))

    assert client.get("/api/admin/mcp/servers").json()["stdio_editable"] is True


def test_restricted_deployments_refuse_every_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client(tmp_path, monkeypatch, restrict_sensitive=True)

    assert client.get("/api/admin/mcp/servers").status_code == 403
    assert client.get("/api/admin/mcp/raw").status_code == 403
    assert (
        client.post(
            "/api/admin/mcp/servers",
            json={"name": "a", "config": {"url": "https://a.example/mcp"}},
        ).status_code
        == 403
    )


def test_restricted_deployments_never_allow_stdio_even_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client(tmp_path, monkeypatch, allow_stdio=True, restrict_sensitive=True)

    assert client.get("/api/admin/mcp/servers").status_code == 403


def test_raw_editor_round_trips_text_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client(tmp_path, monkeypatch)
    content = '{\n    "mcpServers": {\n        "a": {"url": "https://a.example/mcp"}\n    }\n}'

    written = client.put("/api/admin/mcp/raw", json={"content": content})
    assert written.status_code == 200

    read_back = client.get("/api/admin/mcp/raw").json()
    assert read_back["content"].rstrip("\n") == content
    assert read_back["path"].endswith("mcp.json")


def test_raw_editor_rejects_invalid_json_and_invalid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, {"mcpServers": {"a": {"url": "https://a.example/mcp"}}})
    client, _ = _client(tmp_path, monkeypatch)

    assert client.put("/api/admin/mcp/raw", json={"content": "{nope"}).status_code == 400
    assert (
        client.put(
            "/api/admin/mcp/raw",
            json={"content": json.dumps({"mcpServers": {"a": {"description": "empty"}}})},
        ).status_code
        == 400
    )
    # The rejected writes left the good config alone.
    assert list(_stored(tmp_path)["mcpServers"]) == ["a"]


def test_test_route_reports_what_the_probe_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kimo_cli.mcp_probe import ProbeResult, ProbeTool

    _write_config(tmp_path, {"mcpServers": {"a": {"url": "https://a.example/mcp"}}})
    client, _ = _client(tmp_path, monkeypatch)

    async def fake_probe(name: str, config: dict[str, Any], **kwargs: Any) -> ProbeResult:
        assert name == "a"
        return ProbeResult(ok=True, tools=[ProbeTool(name="search", description="Search stuff")])

    monkeypatch.setattr(mcp, "probe_mcp_server", fake_probe)

    body = client.post("/api/admin/mcp/servers/a/test").json()

    assert body == {
        "ok": True,
        "tools": [{"name": "search", "description": "Search stuff"}],
        "error": None,
    }


def test_test_route_reports_failure_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kimo_cli.mcp_probe import ProbeResult

    _write_config(tmp_path, {"mcpServers": {"a": {"url": "https://a.example/mcp"}}})
    client, _ = _client(tmp_path, monkeypatch)

    async def fake_probe(name: str, config: dict[str, Any], **kwargs: Any) -> ProbeResult:
        return ProbeResult(ok=False, error="ConnectionError: refused")

    monkeypatch.setattr(mcp, "probe_mcp_server", fake_probe)

    response = client.post("/api/admin/mcp/servers/a/test")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "tools": [], "error": "ConnectionError: refused"}
    assert client.post("/api/admin/mcp/servers/missing/test").status_code == 404


def test_broken_config_file_is_reported_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "share" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    client, _ = _client(tmp_path, monkeypatch)

    response = client.get("/api/admin/mcp/servers")

    assert response.status_code == 400
    assert "Invalid JSON" in response.json()["detail"]


def test_raw_editor_cannot_hide_a_stdio_server_outside_mcpservers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fastmcp reads a document without "mcpServers" as servers at the root, so
    # this entry would still be launched by every session that starts.
    client, fake = _client(tmp_path, monkeypatch, allow_stdio=False)

    response = client.put(
        "/api/admin/mcp/raw",
        json={"content": json.dumps({"evil": {"command": "touch", "args": ["/tmp/pwned"]}})},
    )

    assert response.status_code == 403
    assert not (tmp_path / "share" / "mcp.json").exists()
    assert fake.calls == []


def test_raw_editor_keeps_a_host_added_stdio_server_it_did_not_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = {"command": "npx", "args": ["some-mcp"]}
    _write_config(tmp_path, {"mcpServers": {"local": local}})
    client, _ = _client(tmp_path, monkeypatch, allow_stdio=False)

    kept = {"mcpServers": {"local": local, "a": {"url": "https://a.example/mcp"}}}
    assert client.put("/api/admin/mcp/raw", json={"content": json.dumps(kept)}).status_code == 200

    changed = {"mcpServers": {"local": {"command": "npx", "args": ["other-mcp"]}}}
    response = client.put("/api/admin/mcp/raw", json={"content": json.dumps(changed)})
    assert response.status_code == 403
    assert _stored(tmp_path) == kept


def test_raw_editor_saves_blank_content_as_an_empty_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, {"mcpServers": {"a": {"url": "https://a.example/mcp"}}})
    client, _ = _client(tmp_path, monkeypatch)

    assert client.put("/api/admin/mcp/raw", json={"content": "  \n"}).status_code == 200

    assert _stored(tmp_path) == {"mcpServers": {}}
    assert client.get("/api/admin/mcp/servers").status_code == 200


def test_test_route_does_not_start_an_oauth_login_on_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(
        tmp_path, {"mcpServers": {"a": {"url": "https://a.example/mcp", "auth": "oauth"}}}
    )
    client, _ = _client(tmp_path, monkeypatch)

    async def no_tokens(url: str) -> bool:
        return False

    async def fail_probe(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("probing an unauthorized OAuth server starts a login")

    monkeypatch.setattr("kimo_cli.mcp_oauth.has_mcp_oauth_tokens", no_tokens)
    monkeypatch.setattr(mcp, "probe_mcp_server", fail_probe)

    body = client.post("/api/admin/mcp/servers/a/test").json()

    assert body["ok"] is False
    assert "kimo mcp auth a" in body["error"]
