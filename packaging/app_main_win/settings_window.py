"""pywebview-backed Settings window for the Windows tray app.

A port of ``packaging.app_main.settings_window`` (AppKit/PyObjC on macOS)
with the same surface area: multi-provider LLM editor (name / type / API key /
base URL / model / max context / capabilities / default), thinking +
temperature, web server port / session token / LAN-only, and branding reset.
The form lives in ``settings.html``; saves write ``LLM_PROVIDERS`` and sync
``config.toml`` exactly like macOS.

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

from . import configtoml, dotenv_io
from . import paths as paths_mod

log = logging.getLogger(__name__)

_HTML_FILE = Path(__file__).resolve().parent / "settings.html"

EXIT_CANCEL = 0
EXIT_SAVE_RESTART = 3
EXIT_SAVE = 4


def _read_toml_default_model(paths: paths_mod.AppPaths) -> str:
    """Read ``default_model`` from kimo-cli's ``config.toml``.

    The web UI's model selector writes the default model to
    ``sessions/config.toml`` (and does not touch ``.env``), so the toml is
    the authoritative source for the default currently in effect. Returns
    "" when the file is missing, unparseable, or has no default set.
    """
    try:
        toml_path = paths.sessions_dir / "config.toml"
        if not toml_path.exists():
            return ""
        import tomlkit

        doc = tomlkit.parse(toml_path.read_text(encoding="utf-8"))
        return str(doc.get("default_model", "") or "").strip()
    except Exception:
        log.exception("failed to read default_model from config.toml")
        return ""


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
        """Return the current ``.env`` contents as a flat dict.

        Also injects ``CONFIG_TOML_DEFAULT_MODEL`` — the ``default_model``
        from kimo-cli's ``config.toml``. The web UI's model selector writes
        there without touching ``.env``, so the JS side prefers it over the
        possibly stale ``LLM_DEFAULT_PROVIDER`` when picking the initial
        default provider. (Not an editable key; ``save_env`` would drop it.)
        """
        try:
            data = dict(dotenv_io.read_editable(self.paths.env_file))
            data["CONFIG_TOML_DEFAULT_MODEL"] = _read_toml_default_model(self.paths)
            return data
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

        # There used to be a rule here promoting Save to Save & Restart when any
        # LLM key moved, because a setting could not otherwise reach a live
        # session. The cost was that changing an API key -- or merely adding a
        # provider -- restarted the server and killed whatever session was
        # mid-turn. The running server applies a saved change itself now: idle
        # sessions immediately, busy ones when their prompt finishes. `restart`
        # arrives True only for the port, which no reload can deliver, and only
        # after the user was asked.

        try:
            dotenv_io.write_env(self.paths.env_file, clean)
        except Exception as e:
            log.exception("write_env failed")
            raise RuntimeError(f"Failed to write .env: {e}") from e

        # Mirror macOS: keep config.toml in sync with the multi-provider blob
        # so edits to base_url / api_key / type / capabilities / default
        # actually take effect in new sessions.
        # `is not None`, not truthiness: an empty list is the answer when the
        # user deleted every provider, and that still has to reach config.toml.
        # The macOS port was fixed for exactly this — skipping the block left
        # [providers.X] / [models.X] / default_model behind, still showing up
        # in the model selector, after .env had already been cleared.
        providers_json = clean.get("LLM_PROVIDERS")
        if providers_json is not None:
            try:
                parsed = [
                    # `or "[]"`: deleting every provider writes LLM_PROVIDERS as
                    # an empty string, not "[]" — json.loads("") raises, the
                    # broad except below swallows it, and the prune this block
                    # exists for never ran. The empty list is the answer here,
                    # not a parse failure.
                    p for p in json.loads(providers_json or "[]")
                    if isinstance(p, dict) and p.get("name")
                ]
                keep_names = {str(p["name"]) for p in parsed}
                # The preserved default may live outside the .env provider
                # list (e.g. a model the user picked via the web selector);
                # keep its toml sections so prune can't leave default_model
                # pointing at a deleted [models.X] entry.
                default_keep = (clean.get("LLM_DEFAULT_PROVIDER") or "").strip()
                if default_keep:
                    keep_names.add(default_keep)
                toml_path = self.paths.sessions_dir / "config.toml"
                configtoml.prune(toml_path, keep_names, allow_empty=True)
                configtoml.update_providers(toml_path, parsed)
                configtoml.update_models(toml_path, parsed)
                configtoml.set_default_model(
                    toml_path, clean.get("LLM_DEFAULT_PROVIDER", "")
                )
            except Exception:
                log.exception("config.toml sync failed; continuing")

        self.exit_code = EXIT_SAVE_RESTART if restart else EXIT_SAVE
        self._destroy_window()

    def reset_branding(self) -> None:
        """Reset Web UI branding to the packaged defaults (mirrors macOS)."""
        from . import seed_branding

        seed_branding.reset_to_packaged(self.paths)

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
