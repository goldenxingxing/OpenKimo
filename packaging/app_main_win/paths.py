"""Path derivation for the Windows OpenKimo installation.

Mirrors :mod:`packaging.app_main.paths` field-for-field where it makes
sense; macOS-only fields (bundle/contents/resources/frameworks/cli_symlink)
are dropped, and all user-state directories move from
``~/Library/...`` to ``%APPDATA%\\<AppName>`` / ``%LOCALAPPDATA%\\<AppName>``.

The expected on-disk layout (installer drops these under
``%LOCALAPPDATA%\\Programs\\OpenKimo`` by default — any prefix works as
long as the relative shape is preserved):

    <install_root>/
        OpenKimo.exe                    # tray launcher
        runtime/
            python/python.exe           # bundled CPython
            kimi_cli/                    # bundled kimi-cli source tree
                kimi_cli/
                    web/static/...
        brand.json
        TrayIcon.ico

User state lives separately at:

    %APPDATA%\\<AppName>\\               # config (.env, sessions, work, output)
    %LOCALAPPDATA%\\<AppName>\\Logs\\    # logs

A few macOS-only fields (``userbase``, ``pip_conf``, ``userbase_bin``,
``bundled_python_bin``) are retained for **parity only** — the macOS
``server.UvicornSupervisor`` is reused unchanged, and it calls
``app_main.userenv.env_overlay(p)`` which reads these. On Windows we
populate them with synthetic paths under ``app_support``; nothing is
actually installed there because the Windows ``userenv`` is a no-op,
but env_overlay still returns a valid dict so uvicorn launches cleanly.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    s = re.sub(r"[^a-z0-9-]", "", s)
    return s or "openkimo"


def _windows_documents_dir() -> Path:
    """Resolve the localized Windows Documents known folder."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            from uuid import UUID

            folder_id = UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7")
            guid = (ctypes.c_ubyte * 16).from_buffer_copy(folder_id.bytes_le)
            raw_path = ctypes.c_wchar_p()
            result = ctypes.windll.shell32.SHGetKnownFolderPath(  # type: ignore[attr-defined]
                ctypes.byref(guid), 0, wintypes.HANDLE(), ctypes.byref(raw_path)
            )
            if result == 0 and raw_path.value:
                try:
                    return Path(raw_path.value)
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(raw_path)  # type: ignore[attr-defined]
        except (AttributeError, OSError, ValueError):
            pass
    return Path.home() / "Documents"


def default_documents_work_dir(app_name: str) -> Path:
    return _windows_documents_dir() / app_name


@dataclass(frozen=True)
class AppPaths:
    # ---- install-side (read-only, bundled with the .exe) ---------------
    install_root: Path          # e.g. %LOCALAPPDATA%\Programs\OpenKimo
    runtime_root: Path          # install_root / "runtime"
    bundled_python_root: Path   # runtime / "python"
    bundled_python_bin: Path    # runtime / "python"   (Scripts/ + python.exe live here on Windows)
    bundled_python: Path        # runtime / "python" / "python.exe"
    app_layer_python: Path      # same as bundled_python on Windows (single layer)
    kimi_cli: Path              # runtime / "kimi_cli" / "kimi_cli"
    static_dir: Path            # kimi_cli / "web" / "static"
    brand_json: Path            # install_root / "brand.json"
    tray_icon: Path             # install_root / "TrayIcon.ico"

    # ---- user-state (read/write, %APPDATA% / %LOCALAPPDATA%) -----------
    app_support: Path           # %APPDATA% / <AppName>
    env_file: Path              # app_support / .env
    pip_conf: Path              # app_support / pip.ini      (parity, unused)
    userbase: Path              # app_support / python-userbase (parity, unused)
    userbase_bin: Path          # userbase / Scripts          (parity, unused)
    work_dir: Path              # localized Documents / <AppName>
    sessions_dir: Path          # app_support / sessions
    output_dir: Path            # app_support / output
    logs: Path                  # %LOCALAPPDATA% / <AppName> / Logs
    server_log: Path            # logs / server.log
    pip_log: Path               # logs / pip.log

    app_name: str               # display name from brand.json
    slug: str                   # lowercase, hyphenated


def _find_install_root() -> Path:
    """Locate the install root on disk.

    Strategy:
      1. Walk up from ``__file__``. If any ancestor has a sibling at
         ``runtime/python/python.exe``, that ancestor is the install root.
      2. Fallback: walk up from ``sys.executable`` (PyInstaller / packaged
         runtime case) looking for the same sentinel.
      3. Dev fallback: the repository root, two levels above this file
         (``packaging/app_main_win/paths.py`` -> ``packaging/`` -> repo).
    """
    sentinel = Path("runtime") / "python" / "python.exe"

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / sentinel).exists():
            return parent

    if sys.executable:
        exe = Path(sys.executable).resolve()
        for parent in exe.parents:
            if (parent / sentinel).exists():
                return parent

    # Dev fallback: repo root.
    return here.parents[2]


def _read_brand(install_root: Path) -> tuple[str, dict]:
    brand_json = install_root / "brand.json"
    if brand_json.exists():
        try:
            data = json.loads(brand_json.read_text(encoding="utf-8"))
            return data.get("app_name", "OpenKimo"), data
        except (OSError, json.JSONDecodeError):
            pass
    return "OpenKimo", {}


def _user_state_root(app_name: str) -> Path:
    """``%APPDATA%\\<AppName>``; falls back to ``~/AppData/Roaming/...`` on
    non-Windows hosts (CI / dev) so this module imports cleanly on macOS too."""
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / app_name
    return Path.home() / "AppData" / "Roaming" / app_name


def _logs_root(app_name: str) -> Path:
    """``%LOCALAPPDATA%\\<AppName>\\Logs``; same dev fallback as above."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / app_name / "Logs"
    return Path.home() / "AppData" / "Local" / app_name / "Logs"


@lru_cache(maxsize=1)
def app_paths() -> AppPaths:
    install_root = _find_install_root()
    app_name, _ = _read_brand(install_root)
    slug = _slugify(app_name)

    runtime_root = install_root / "runtime"
    py_root = runtime_root / "python"
    # On Windows, python.exe and Scripts/ both live directly under the python
    # root (no bin/ subdir). We point bundled_python_bin at the python root so
    # ``server._build_env`` -> ``userenv.env_overlay`` PATH addition resolves
    # to a real directory containing python.exe.
    py_exe = py_root / "python.exe"
    kimi_cli = runtime_root / "kimi_cli" / "kimi_cli"

    app_support = _user_state_root(app_name)
    logs = _logs_root(app_name)
    userbase = app_support / "python-userbase"

    return AppPaths(
        install_root=install_root,
        runtime_root=runtime_root,
        bundled_python_root=py_root,
        bundled_python_bin=py_root,
        bundled_python=py_exe,
        app_layer_python=py_exe,
        kimi_cli=kimi_cli,
        static_dir=kimi_cli / "web" / "static",
        brand_json=install_root / "brand.json",
        tray_icon=install_root / "TrayIcon.ico",
        app_support=app_support,
        env_file=app_support / ".env",
        pip_conf=app_support / "pip.ini",
        userbase=userbase,
        userbase_bin=userbase / "Scripts",
        work_dir=default_documents_work_dir(app_name),
        sessions_dir=app_support / "sessions",
        output_dir=app_support / "output",
        logs=logs,
        server_log=logs / "server.log",
        pip_log=logs / "pip.log",
        app_name=app_name,
        slug=slug,
    )


def ensure_dirs() -> AppPaths:
    p = app_paths()
    for d in (p.app_support, p.work_dir, p.sessions_dir, p.output_dir, p.logs):
        d.mkdir(parents=True, exist_ok=True)
    return p


def load_brand_json() -> dict:
    p = app_paths()
    if p.brand_json.exists():
        try:
            return json.loads(p.brand_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def env_file_path() -> Path:
    """Public helper used by Settings/dotenv_io and the supervisor."""
    return app_paths().env_file


__all__ = ["AppPaths", "app_paths", "ensure_dirs", "load_brand_json", "env_file_path"]
