from __future__ import annotations

import importlib
from pathlib import Path


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


def test_settings_do_not_expose_path_controls() -> None:
    root = Path(__file__).parents[1]
    mac_source = (root / "packaging/app_main/settings_window.py").read_text()
    windows_html = (root / "packaging/app_main_win/settings.html").read_text()
    editable_source = (root / "packaging/app_main/dotenv_io.py").read_text()

    assert "_build_paths_section" not in mac_source
    assert "<h2>Paths</h2>" not in windows_html
    assert 'id="work_dir"' not in windows_html
    assert '"KIMI_DEFAULT_WORK_DIR",' not in editable_source
    assert '"CUSTOM_SKILLS_HOST_PATH",' not in editable_source
