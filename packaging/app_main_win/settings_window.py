"""pywebview-backed Settings window for the Windows tray app.

A V1 port of ``packaging.app_main.settings_window`` (which is a 1370-line
AppKit/PyObjC window). We deliberately ship a much smaller surface area:
just the single-provider fields most users actually need. The multi-provider
JSON editor on macOS is omitted here and will land in a later iteration.

This module is meant to be run as a standalone subprocess:

    python -m packaging.app_main_win.settings_window

The tray supervisor (``__main__.py``) spawns it that way so pywebview gets
its own main thread (it cannot share one with pystray). The subprocess
exits with one of three codes:

    0   user cancelled / closed the window
    3   user clicked "Save & Restart" — tray respawns uvicorn
    4   user clicked "Save"            — tray starts uvicorn if it was idle
                                          (first-run flow); otherwise no-op

The actual ``.env`` write happens here in-process via :mod:`dotenv_io`.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from . import configtoml, dotenv_io, paths as paths_mod

log = logging.getLogger(__name__)

_HTML_FILE = Path(__file__).resolve().parent / "settings.html"

EXIT_CANCEL = 0
EXIT_SAVE_RESTART = 3
EXIT_SAVE = 4


class Api:
    """Python side of the pywebview JS bridge.

    All public methods are callable from the renderer as
    ``window.pywebview.api.<name>(...)``.
    """

    def __init__(self, paths: paths_mod.AppPaths):
        self.paths = paths
        self.exit_code = EXIT_CANCEL
        self._window: Any | None = None  # set after window creation

    def attach_window(self, window: Any) -> None:
        self._window = window

    # ---- JS-callable API ---------------------------------------------

    def load_env(self) -> dict[str, str]:
        """Return the current ``.env`` contents as a flat dict."""
        try:
            return dict(dotenv_io.read_editable(self.paths.env_file))
        except Exception:
            log.exception("load_env failed")
            return {}

    def save_env(self, updates: dict[str, str], restart: bool) -> None:
        """Persist ``updates`` to ``.env`` and close the window.

        ``restart=True`` exits with code 3 so the tray supervisor knows to
        respawn uvicorn. Otherwise we exit with code 4 (just-saved).
        """
        clean: dict[str, str] = {}
        for k, v in (updates or {}).items():
            if k not in dotenv_io.EDITABLE_KEYS:
                # Silently drop unknown keys — the HTML form might evolve
                # ahead of the whitelist during development.
                continue
            clean[k] = "" if v is None else str(v)

        # LLM settings only reach a session worker when it (re)starts, so a
        # plain Save would leave every live session on the old provider
        # config indefinitely. Promote to restart when an LLM key changed.
        if not restart:
            try:
                current = dotenv_io.read_env(self.paths.env_file)
                restart = any(
                    (clean.get(k) or "") != (current.get(k) or "")
                    for k in clean
                    if k.startswith(("LLM_", "KIMI_API", "KIMI_BASE", "KIMI_MODEL",
                                     "OPENAI_", "ANTHROPIC_"))
                )
                if restart:
                    log.info("LLM config changed; promoting Save to Save & Restart")
            except Exception:
                log.exception("failed to diff LLM keys; leaving restart flag as-is")

        try:
            dotenv_io.write_env(self.paths.env_file, clean)
        except Exception as e:
            log.exception("write_env failed")
            raise RuntimeError(f"Failed to write .env: {e}") from e

        # Mirror macOS: keep config.toml in sync if the user is editing the
        # multi-provider blob. V1 UI only edits legacy single-provider keys
        # so this is a no-op unless the user manually populated LLM_PROVIDERS
        # elsewhere — but we leave the hook in for forward-compatibility.
        providers_json = clean.get("LLM_PROVIDERS", "")
        if providers_json:
            try:
                parsed = [
                    p for p in json.loads(providers_json)
                    if isinstance(p, dict) and p.get("name")
                ]
                keep_names = {str(p["name"]) for p in parsed}
                toml_path = self.paths.sessions_dir / "config.toml"
                if keep_names:
                    configtoml.prune(toml_path, keep_names)
                    configtoml.update_providers(toml_path, parsed)
                    configtoml.update_models(toml_path, parsed)
                    configtoml.set_default_model(
                        toml_path, clean.get("LLM_DEFAULT_PROVIDER", "")
                    )
            except Exception:
                log.exception("config.toml sync failed; continuing")

        self.exit_code = EXIT_SAVE_RESTART if restart else EXIT_SAVE
        self._destroy_window()

    def cancel(self) -> None:
        self.exit_code = EXIT_CANCEL
        self._destroy_window()

    # ---- helpers ------------------------------------------------------

    def _destroy_window(self) -> None:
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                log.exception("window.destroy failed")


def open_settings() -> int:
    """Open the Settings window (blocking) and return the chosen exit code.

    Importable from the tray app for tests / in-process invocation, though
    in production the tray spawns a subprocess (see module docstring).
    """
    import webview

    paths = paths_mod.app_paths()
    paths_mod.ensure_dirs()

    api = Api(paths)
    try:
        html = _HTML_FILE.read_text(encoding="utf-8")
    except OSError:
        log.exception("settings.html missing at %s", _HTML_FILE)
        return EXIT_CANCEL

    window = webview.create_window(
        title=f"{paths.app_name} Settings",
        html=html,
        js_api=api,
        width=720,
        height=640,
        min_size=(560, 480),
        resizable=True,
    )
    api.attach_window(window)
    # ``gui=None`` lets pywebview pick the best Windows backend (EdgeChromium
    # by default on Win10+). The call blocks until the window closes.
    webview.start()
    return api.exit_code


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return open_settings()
    except Exception:
        log.exception("settings window crashed")
        return EXIT_CANCEL


if __name__ == "__main__":
    sys.exit(main())
