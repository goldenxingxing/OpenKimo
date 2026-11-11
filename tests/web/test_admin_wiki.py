"""The admin Wiki API edits pages through the same transaction as the Wiki tool."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kimo_cli.web.api import admin_wiki
from kimo_cli.web.user_auth import require_admin
from kimo_cli.wiki.manager import WikiManager


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    manager = WikiManager(tmp_path / "wiki", wal=False)
    monkeypatch.setattr(admin_wiki, "_wiki_manager", lambda: manager)
    app = FastAPI()
    app.include_router(admin_wiki.router)
    app.dependency_overrides[require_admin] = lambda: {"id": "admin", "role": "admin"}
    yield TestClient(app)
    manager.close()


def test_admin_can_create_read_edit_search_and_delete(client: TestClient) -> None:
    created = client.put(
        "/api/admin/wiki/pages/concepts/发布流程.md",
        json={"title": "发布流程", "tags": ["process"], "body": "每两周周二发布。\n"},
    )
    assert created.status_code == 200, created.text
    assert created.json() == {"global_revision": 1, "pages": ["concepts/发布流程.md"]}

    overview = client.get("/api/admin/wiki").json()
    assert overview["global_revision"] == 1
    assert "concepts" in overview["categories"]
    [summary] = overview["pages"]
    assert summary["path"] == "concepts/发布流程.md"
    assert summary["category"] == "concepts"
    assert summary["revision"] == 1

    page = client.get("/api/admin/wiki/pages/concepts/发布流程.md").json()
    assert page["title"] == "发布流程"
    assert page["body"] == "每两周周二发布。\n"
    assert page["sources"] == []

    stale = client.put(
        "/api/admin/wiki/pages/concepts/发布流程.md",
        json={"title": "发布流程", "body": "改。\n", "expected_revision": 7},
    )
    assert stale.status_code == 409

    edited = client.put(
        "/api/admin/wiki/pages/concepts/发布流程.md",
        json={
            "title": "发布流程",
            "tags": [],
            "body": "每两周周三发布。\n",
            "expected_revision": 1,
        },
    )
    assert edited.status_code == 200
    assert client.get("/api/admin/wiki/pages/concepts/发布流程.md").json()["revision"] == 2

    hits = client.get("/api/admin/wiki/search", params={"q": "周三"}).json()
    assert [hit["path"] for hit in hits] == ["concepts/发布流程.md"]

    log = client.get("/api/admin/wiki/log").json()
    assert [entry["operation"] for entry in log] == ["admin-edit", "admin-create"]
    assert log[0]["revision"] == 2
    assert log[0]["pages"] == ["concepts/发布流程.md"]

    assert (
        client.delete(
            "/api/admin/wiki/pages/concepts/发布流程.md", params={"expected_revision": 1}
        ).status_code
        == 409
    )
    deleted = client.delete(
        "/api/admin/wiki/pages/concepts/发布流程.md", params={"expected_revision": 2}
    )
    assert deleted.status_code == 204
    assert client.get("/api/admin/wiki").json()["pages"] == []
    assert client.get("/api/admin/wiki/pages/concepts/发布流程.md").status_code == 404


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("concepts/../escape.md", "Fine.\n"),
        ("elsewhere/page.md", "Fine.\n"),
        ("concepts/leak.md", "api_key = sk-abcdefghijklmnopqrstuvwxyz0123\n"),
        ("concepts/local.md", "Lives in /Users/someone/project.\n"),
        ("concepts/empty.md", "   \n"),
    ],
)
def test_pages_that_cannot_be_stored_are_refused(client: TestClient, path: str, body: str) -> None:
    response = client.put(f"/api/admin/wiki/pages/{path}", json={"title": "T", "body": body})

    assert response.status_code in {404, 422}, response.text
    assert client.get("/api/admin/wiki").json()["global_revision"] == 0


def test_lint_reports_a_malformed_page(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "wiki" / "entities" / "broken.md").write_text("no frontmatter\n", "utf-8")

    report = client.get("/api/admin/wiki/lint").json()
    overview = client.get("/api/admin/wiki").json()

    assert any(
        issue["code"] == "malformed_page" and issue["path"] == "entities/broken.md"
        for issue in report["issues"]
    )
    [page] = overview["pages"]
    assert page["title"] is None
    assert page["error"]


def test_unconfigured_wiki_is_a_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENKIMO_WIKI_ROOT", raising=False)
    monkeypatch.delenv("OPENKIMO_APP_DATA_DIR", raising=False)
    monkeypatch.setattr(admin_wiki, "_manager", None)
    app = FastAPI()
    app.include_router(admin_wiki.router)
    app.dependency_overrides[require_admin] = lambda: {"id": "admin", "role": "admin"}

    response = TestClient(app).get("/api/admin/wiki")

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]
