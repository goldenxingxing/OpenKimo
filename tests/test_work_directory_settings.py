from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace


def test_macos_default_work_directory_is_documents_openkimo(monkeypatch, tmp_path: Path) -> None:
    paths = importlib.import_module("packaging.app_main.paths")
    paths.app_paths.cache_clear()
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))

    assert paths.default_documents_work_dir("OpenKimo") == tmp_path / "Documents" / "OpenKimo"


def test_windows_default_work_directory_uses_documents_resolver(
    monkeypatch, tmp_path: Path
) -> None:
    paths = importlib.import_module("packaging.app_main_win.paths")
    documents = tmp_path / "文档"
    monkeypatch.setattr(paths, "_windows_documents_dir", lambda: documents)

    assert paths.default_documents_work_dir("OpenKimo") == documents / "OpenKimo"


def test_macos_creates_global_user_wiki_directory(monkeypatch, tmp_path: Path) -> None:
    paths = importlib.import_module("packaging.app_main.paths")
    paths.app_paths.cache_clear()
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))

    resolved = paths.ensure_dirs()

    assert resolved.wiki_dir == (
        tmp_path / "Library" / "Application Support" / "OpenKimo" / "users" / "default" / "wiki"
    )
    assert resolved.wiki_dir.is_dir()
    paths.app_paths.cache_clear()


def test_windows_creates_global_user_wiki_directory(monkeypatch, tmp_path: Path) -> None:
    paths = importlib.import_module("packaging.app_main_win.paths")
    paths.app_paths.cache_clear()
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(paths, "_windows_documents_dir", lambda: tmp_path / "Documents")

    resolved = paths.ensure_dirs()

    assert resolved.wiki_dir == tmp_path / "Roaming" / "OpenKimo" / "users" / "default" / "wiki"
    assert resolved.wiki_dir.is_dir()
    paths.app_paths.cache_clear()


def test_settings_do_not_expose_path_controls() -> None:
    root = Path(__file__).parents[1]
    mac_source = (root / "packaging/app_main/settings_window.py").read_text()
    windows_html = (root / "packaging/app_main_win/settings.html").read_text()
    editable_source = (root / "packaging/app_main/dotenv_io.py").read_text()

    assert "_build_paths_section" not in mac_source
    assert "<h2>Paths</h2>" not in windows_html
    assert 'id="work_dir"' not in windows_html
    assert '"KIMO_DEFAULT_WORK_DIR",' not in editable_source
    assert '"CUSTOM_SKILLS_HOST_PATH",' not in editable_source


def test_legacy_env_cannot_override_managed_desktop_paths(
    monkeypatch, tmp_path: Path
) -> None:
    server = importlib.import_module("packaging.app_main.server")
    paths = SimpleNamespace(
        app_name="OpenKimo",
        static_dir=tmp_path / "static",
        app_support=tmp_path / "state",
        work_dir=tmp_path / "Documents" / "OpenKimo",
        sessions_dir=tmp_path / "state" / "sessions",
        output_dir=tmp_path / "legacy-output",
        skill_dir=tmp_path / "skill",
        wiki_dir=tmp_path / "users" / "default" / "wiki",
        kimo_cli=tmp_path / "runtime" / "kimo_cli",
        env_file=tmp_path / ".env",
    )
    monkeypatch.setattr(server.userenv, "env_overlay", lambda _paths: {})
    monkeypatch.setattr(
        server.dotenv_io,
        "read_env",
        lambda _path: {
            "KIMO_DEFAULT_WORK_DIR": "/old/work",
            "KIMO_SHARE_DIR": "/old/sessions",
            "KIMO_OUTPUT_DIR": "/old/output",
            "OPENKIMO_SKILL_DIR": "/old/skills",
            "OPENKIMO_APP_DATA_DIR": "/old/app-data",
            "OPENKIMO_WIKI_ROOT": "/old/wiki",
        },
    )

    env = server._build_env(paths)

    assert env["KIMO_DEFAULT_WORK_DIR"] == str(paths.work_dir)
    assert env["KIMO_SHARE_DIR"] == str(paths.sessions_dir)
    assert env["KIMO_OUTPUT_DIR"] == str(paths.output_dir)
    assert env["OPENKIMO_SKILL_DIR"] == str(paths.skill_dir)
    assert env["OPENKIMO_APP_DATA_DIR"] == str(paths.app_support)
    assert env["OPENKIMO_WIKI_ROOT"] == str(paths.wiki_dir)
    assert env["KIMO_AGENT_NAME"] == "OpenKimo"


def test_agent_identity_follows_app_brand(monkeypatch, tmp_path: Path) -> None:
    """The in-conversation identity must follow the branded app name."""
    server = importlib.import_module("packaging.app_main.server")
    paths = SimpleNamespace(
        app_name="ACME Agent",
        static_dir=tmp_path / "static",
        app_support=tmp_path / "state",
        work_dir=tmp_path / "Documents" / "ACME Agent",
        sessions_dir=tmp_path / "state" / "sessions",
        output_dir=tmp_path / "output",
        skill_dir=tmp_path / "skill",
        wiki_dir=tmp_path / "users" / "default" / "wiki",
        kimo_cli=tmp_path / "runtime" / "kimo_cli",
        env_file=tmp_path / ".env",
    )
    monkeypatch.setattr(server.userenv, "env_overlay", lambda _paths: {})
    monkeypatch.setattr(server.dotenv_io, "read_env", lambda _path: {})

    env = server._build_env(paths)

    assert env["KIMO_AGENT_NAME"] == "ACME Agent"
