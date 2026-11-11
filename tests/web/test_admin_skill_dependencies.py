"""An uploaded skill arrives without its packages; the panel says so and installs them."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kimo_cli.skill import manager as manager_module
from kimo_cli.skill.manager import SkillManager
from kimo_cli.web.api import admin
from kimo_cli.web.user_auth import require_admin

_SKILL_MD = "---\nname: mailer\ndescription: Sends mail\n---\n"
_PACKAGE = {"name": "mailer", "dependencies": {"dotenv": "^16", "@scope/imap": "^1"}}


def _archive(*, lockfile: bool = True) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("mailer/SKILL.md", _SKILL_MD)
        archive.writestr("mailer/package.json", json.dumps(_PACKAGE))
        if lockfile:
            archive.writestr("mailer/package-lock.json", "{}")
    return stream.getvalue()


def _fake_npm(tmp_path: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    """A stand-in npm that records its arguments and lays out node_modules."""
    record = tmp_path / "npm-args.txt"
    script = tmp_path / "npm"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" > "{record}"\n'
        f"if [ {exit_code} -ne 0 ]; then echo 'ERR! network down' >&2; exit {exit_code}; fi\n"
        "for dep in dotenv @scope/imap; do\n"
        '  mkdir -p "node_modules/$dep" && echo "{}" > "node_modules/$dep/package.json"\n'
        '  for i in 1 2 3; do echo x > "node_modules/$dep/f$i.js"; done\n'
        "done\n"
    )
    script.chmod(0o755)
    return script, record


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    factory = builtin / "factory"
    factory.mkdir()
    (factory / "SKILL.md").write_text("---\nname: factory\ndescription: F\n---\n", "utf-8")
    (factory / "package.json").write_text(json.dumps(_PACKAGE), "utf-8")
    writable = tmp_path / "skill"
    monkeypatch.setattr(admin, "_skill_manager", lambda: SkillManager(builtin, writable))
    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[require_admin] = lambda: {"id": "admin", "role": "admin"}
    return TestClient(app)


def _upload(client: TestClient, data: bytes) -> dict:
    response = client.post(
        "/api/admin/skills/upload", files={"file": ("mailer.zip", data, "application/zip")}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_an_uploaded_skill_reports_the_packages_it_is_missing(client: TestClient) -> None:
    skill = _upload(client, _archive())

    assert skill["dependencies"] == {
        "manager": "npm",
        "manifest": "package.json",
        "missing": ["@scope/imap", "dotenv"],
        "installable": True,
    }


def test_installing_fills_node_modules_without_listing_it(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _upload(client, _archive())
    npm, record = _fake_npm(tmp_path)
    monkeypatch.setattr(manager_module.shutil, "which", lambda _name: str(npm))

    response = client.post("/api/admin/skills/mailer/install-dependencies")

    assert response.status_code == 200, response.text
    skill = response.json()
    assert skill["dependencies"]["missing"] == []
    assert skill["files"] == ["SKILL.md", "package-lock.json", "package.json"]
    args = record.read_text().split()
    assert args[0] == "ci"
    assert "--ignore-scripts" in args


def test_without_a_lockfile_npm_install_is_used(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _upload(client, _archive(lockfile=False))
    npm, record = _fake_npm(tmp_path)
    monkeypatch.setattr(manager_module.shutil, "which", lambda _name: str(npm))

    assert client.post("/api/admin/skills/mailer/install-dependencies").status_code == 200
    assert record.read_text().split()[0] == "install"


def test_a_failed_install_says_what_npm_said(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _upload(client, _archive())
    npm, _ = _fake_npm(tmp_path, exit_code=1)
    monkeypatch.setattr(manager_module.shutil, "which", lambda _name: str(npm))

    response = client.post("/api/admin/skills/mailer/install-dependencies")

    assert response.status_code == 400
    assert "network down" in response.json()["detail"]
    listed = next(s for s in client.get("/api/admin/skills").json() if s["name"] == "mailer")
    assert listed["dependencies"]["missing"] == ["@scope/imap", "dotenv"]


def test_missing_npm_is_named(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _upload(client, _archive())
    monkeypatch.setattr(manager_module.shutil, "which", lambda _name: None)

    response = client.post("/api/admin/skills/mailer/install-dependencies")

    assert response.status_code == 400
    assert "npm was not found" in response.json()["detail"]


def test_built_in_skills_are_read_only(client: TestClient) -> None:
    listed = next(s for s in client.get("/api/admin/skills").json() if s["name"] == "factory")
    assert listed["dependencies"]["installable"] is False

    response = client.post("/api/admin/skills/factory/install-dependencies")

    assert response.status_code == 400
    assert "read-only" in response.json()["detail"]


def test_a_skill_without_packages_has_nothing_to_install(client: TestClient) -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("plain/SKILL.md", "---\nname: plain\ndescription: P\n---\n")
    skill = _upload(client, stream.getvalue())

    assert skill["dependencies"] is None
    assert client.post("/api/admin/skills/plain/install-dependencies").status_code == 400
