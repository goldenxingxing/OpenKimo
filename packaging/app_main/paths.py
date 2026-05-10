"""Path derivation from the running .app bundle.

All user-facing directories follow `<AppName>` (read from Info.plist)
so a white-label rebrand cleanly isolates its own state.
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


@dataclass(frozen=True)
class AppPaths:
    bundle: Path                # OpenKimo.app/
    contents: Path              # OpenKimo.app/Contents
    resources: Path             # OpenKimo.app/Contents/Resources
    macos: Path                 # OpenKimo.app/Contents/MacOS
    frameworks: Path            # OpenKimo.app/Contents/Frameworks
    bundled_python_root: Path   # runtimes/cpython-3.12
    bundled_python_bin: Path    # runtimes/cpython-3.12/bin
    bundled_python: Path        # runtimes/cpython-3.12/bin/python3
    app_layer_python: Path      # runtimes/app-*/bin/python  (sees all 3 layers)
    app_main: Path              # Resources/app_main
    kimi_cli: Path              # Resources/kimi_cli
    static_dir: Path            # Resources/kimi_cli/web/static
    brand_json: Path            # Resources/brand.json
    menubar_icon: Path          # Resources/MenuBarIcon.png (44x44 status-bar icon)

    app_support: Path           # ~/Library/Application Support/<AppName>
    env_file: Path              # ~/Library/Application Support/<AppName>/.env
    pip_conf: Path              # ~/Library/Application Support/<AppName>/pip.conf
    userbase: Path              # ~/Library/Application Support/<AppName>/python-userbase
    userbase_bin: Path          # userbase/bin
    work_dir: Path              # app_support/work
    sessions_dir: Path          # app_support/sessions
    output_dir: Path            # app_support/output
    logs: Path                  # ~/Library/Logs/<AppName>
    server_log: Path            # logs/server.log
    pip_log: Path               # logs/pip.log

    app_name: str               # display name from Info.plist
    slug: str                   # lowercase, hyphenated; used for cli command

    @property
    def cli_symlink(self) -> Path:
        return Path.home() / ".local" / "bin" / self.slug

    @property
    def cli_wrapper(self) -> Path:
        return self.resources / f"{self.slug}-cli"


def _find_bundle_root() -> Path:
    """Locate <AppName>.app on disk.

    The C launcher passes the resolved app_main directory as the script
    argument to Py_BytesMain, so it shows up as ``sys.argv[0]``. Walking
    up from ``__file__`` is the more reliable path inside the bundle.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.suffix == ".app":
            return parent
    if sys.argv and sys.argv[0]:
        candidate = Path(sys.argv[0]).resolve()
        if candidate.is_dir() and candidate.name == "app_main":
            return candidate.parent.parent.parent  # Resources -> Contents -> .app
    # Dev fallback: pretend the repo root is the bundle (paths still resolve
    # to ~/Library/Application Support/OpenKimo).
    return here.parents[2]


def _read_brand(resources: Path) -> tuple[str, dict]:
    brand_json = resources / "brand.json"
    if brand_json.exists():
        data = json.loads(brand_json.read_text())
        return data.get("app_name", "OpenKimo"), data
    info_plist = resources.parent / "Info.plist"
    if info_plist.exists():
        try:
            import plistlib
            with info_plist.open("rb") as f:
                pl = plistlib.load(f)
            return pl.get("CFBundleName", "OpenKimo"), {}
        except Exception:
            pass
    return "OpenKimo", {}


@lru_cache(maxsize=1)
def app_paths() -> AppPaths:
    bundle = _find_bundle_root()
    contents = bundle / "Contents"
    resources = contents / "Resources"
    app_name, _ = _read_brand(resources)
    slug = _slugify(app_name)

    py_root = resources / "runtimes" / "cpython-3.12"
    runtimes_dir = resources / "runtimes"
    # The application layer is named "app-<name>" in venvstacks.toml, then
    # export prefixes with another "app-", e.g. "app-app-openkimo". Pick it
    # up dynamically so white-label rebrands don't have to patch this file.
    app_layer_root = next(
        (p for p in runtimes_dir.glob("app-*") if p.is_dir()),
        py_root,
    ) if runtimes_dir.exists() else py_root
    app_support = Path.home() / "Library" / "Application Support" / app_name
    logs = Path.home() / "Library" / "Logs" / app_name
    userbase = app_support / "python-userbase"

    return AppPaths(
        bundle=bundle,
        contents=contents,
        resources=resources,
        macos=contents / "MacOS",
        frameworks=contents / "Frameworks",
        bundled_python_root=py_root,
        bundled_python_bin=py_root / "bin",
        bundled_python=py_root / "bin" / "python3",
        app_layer_python=app_layer_root / "bin" / "python",
        app_main=resources / "app_main",
        kimi_cli=resources / "kimi_cli",
        static_dir=resources / "kimi_cli" / "web" / "static",
        brand_json=resources / "brand.json",
        menubar_icon=resources / "MenuBarIcon.png",
        app_support=app_support,
        env_file=app_support / ".env",
        pip_conf=app_support / "pip.conf",
        userbase=userbase,
        userbase_bin=userbase / "bin",
        work_dir=app_support / "work",
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
    for d in (p.app_support, p.work_dir, p.sessions_dir, p.output_dir,
              p.userbase, p.userbase_bin, p.logs):
        d.mkdir(parents=True, exist_ok=True)
    return p


def load_brand_json() -> dict:
    p = app_paths()
    if p.brand_json.exists():
        try:
            return json.loads(p.brand_json.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def env_file_path() -> Path:
    """Public helper used by Settings/dotenv_io and supervisor."""
    return app_paths().env_file


# Re-exports used heavily by other app_main modules.
__all__ = ["AppPaths", "app_paths", "ensure_dirs", "load_brand_json", "env_file_path"]
