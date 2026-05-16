"""System-tray entry point for the OpenKimo Windows package.

Loaded as ``python -m packaging.app_main_win`` (or wrapped by ``OpenKimo.exe``).

Responsibilities (parallels ``packaging.app_main.__main__``):
  * seed Web UI branding into SQLite on first run
  * supervise the uvicorn child process via ``UvicornSupervisor``
  * render a system-tray icon + menu (pystray)
  * spawn the Settings window as a subprocess (pywebview must own its own
    main thread; running it inline would deadlock with the tray loop)

We deliberately keep this file structurally close to the macOS counterpart
so the two stay easy to diff. The differences vs macOS are:
  * ``rumps.App`` -> ``pystray.Icon``
  * ``subprocess.run(["open", ...])`` -> ``os.startfile(...)``
  * ``AppKit`` Settings window -> ``settings_window`` subprocess
  * No "Install Package…" / "Open Terminal Here" menu entries (deferred)
  * No edit-menu install (Windows handles standard shortcuts natively)
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import pystray
from PIL import Image

from . import dotenv_io, paths as paths_mod, seed_branding, userenv
from .server import ServerState, UvicornSupervisor

log = logging.getLogger(__name__)


# ---------- status helpers -----------------------------------------------

_ICON_GLYPHS = {
    ServerState.RUNNING: "Running",
    ServerState.STARTING: "Starting",
    ServerState.STOPPED: "Stopped",
    ServerState.CRASHED: "Crashed",
}


def _status_line(state: ServerState, port: int) -> str:
    label = _ICON_GLYPHS.get(state, "?")
    if state == ServerState.RUNNING:
        return f"{label} · port {port}"
    if state == ServerState.STARTING:
        return f"{label}…"
    if state == ServerState.CRASHED:
        return f"{label} (see logs)"
    return label


# ---------- tray app ------------------------------------------------------

class OpenKimoApp:
    """System-tray supervisor.

    Owns a single ``pystray.Icon`` instance plus the ``UvicornSupervisor``.
    All state mutations originate either from menu callbacks (running on
    pystray's UI thread) or from the supervisor's watcher thread; both
    funnel through ``_on_state_change`` which calls ``icon.update_menu()``.
    """

    def __init__(self, p: paths_mod.AppPaths):
        self.paths = p
        self.supervisor = UvicornSupervisor(p, on_state_change=self._on_state_change)
        self._opened_once = False
        self._settings_proc: subprocess.Popen | None = None

        # Tray icon image. Fall back to a generated solid square if the
        # packaged .ico is missing — pystray requires a PIL Image.
        if p.tray_icon.exists():
            try:
                image = Image.open(str(p.tray_icon))
            except Exception:
                log.exception("failed to open tray icon %s", p.tray_icon)
                image = self._fallback_icon()
        else:
            log.warning("tray icon missing at %s; using fallback", p.tray_icon)
            image = self._fallback_icon()

        self.icon = pystray.Icon(
            name=p.slug,
            title=p.app_name,
            icon=image,
            menu=self._build_menu(),
        )

        # Run blocking startup work off the pystray UI thread.
        self._worker = threading.Thread(
            target=self._startup, name="kimi-startup", daemon=True
        )

    # ---- menu construction --------------------------------------------

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                lambda _: _status_line(self.supervisor.state, self.supervisor.port),
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Open Web UI",
                self._on_open_web,
                enabled=lambda _: self.supervisor.state == ServerState.RUNNING,
                default=True,
            ),
            pystray.MenuItem(
                "Open Admin",
                self._on_open_admin,
                enabled=lambda _: self.supervisor.state == ServerState.RUNNING,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda _: self._toggle_label(),
                self._on_toggle,
            ),
            pystray.MenuItem(
                "Restart Server",
                self._on_restart,
                enabled=lambda _: self.supervisor.state
                in (ServerState.RUNNING, ServerState.STARTING),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings…", self._on_settings),
            pystray.MenuItem("Open Config Folder", self._on_open_config),
            pystray.MenuItem("View Logs", self._on_view_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"About {self.paths.app_name}", self._on_about),
            pystray.MenuItem(f"Quit {self.paths.app_name}", self._on_quit),
        )

    def _toggle_label(self) -> str:
        if self.supervisor.state in (ServerState.RUNNING, ServerState.STARTING):
            return "Stop Server"
        return "Start Server"

    @staticmethod
    def _fallback_icon() -> Image.Image:
        img = Image.new("RGBA", (32, 32), (40, 40, 40, 255))
        return img

    # ---- startup ------------------------------------------------------

    def run(self) -> None:
        """Block on the tray event loop. Spawns the startup worker first."""
        self._worker.start()
        self.icon.run()

    def _startup(self) -> None:
        try:
            userenv.setup(self.paths)
        except Exception:
            log.exception("userenv.setup failed")
        try:
            seed_branding.seed_if_needed(self.paths)
        except Exception:
            log.exception("seed_branding failed")

        if self._needs_setup():
            # Open Settings first; user will click Save which writes .env,
            # then we start the supervisor below.
            self._on_settings(None)
            return
        self.supervisor.start()

    def _needs_setup(self) -> bool:
        env = dotenv_io.read_env(self.paths.env_file)
        if (env.get("LLM_PROVIDERS") or "").strip():
            return False
        return not any(
            env.get(k) for k in ("KIMI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
        )

    # ---- supervisor callback ------------------------------------------

    def _on_state_change(self, state: ServerState, info: dict) -> None:
        port = info.get("port", self.supervisor.port)
        log.info("server state -> %s (port %s)", state.value, port)
        try:
            self.icon.update_menu()
        except Exception:
            log.exception("icon.update_menu failed")
        if state == ServerState.RUNNING:
            self._maybe_open_browser(port)

    def _maybe_open_browser(self, port: int) -> None:
        if self._opened_once:
            return
        self._opened_once = True
        try:
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:
            log.exception("webbrowser.open failed")

    # ---- menu handlers ------------------------------------------------

    def _on_open_web(self, _icon, _item) -> None:
        webbrowser.open(f"http://127.0.0.1:{self.supervisor.port}")

    def _on_open_admin(self, _icon, _item) -> None:
        webbrowser.open(f"http://127.0.0.1:{self.supervisor.port}/admin")

    def _on_toggle(self, _icon, _item) -> None:
        if self.supervisor.state in (ServerState.RUNNING, ServerState.STARTING):
            threading.Thread(target=self.supervisor.stop, daemon=True).start()
        else:
            threading.Thread(target=self.supervisor.start, daemon=True).start()

    def _on_restart(self, _icon, _item) -> None:
        threading.Thread(target=self.supervisor.restart, daemon=True).start()

    def _on_open_config(self, _icon, _item) -> None:
        self.paths.app_support.mkdir(parents=True, exist_ok=True)
        self._open_in_explorer(self.paths.app_support)

    def _on_view_logs(self, _icon, _item) -> None:
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        target = self.paths.server_log if self.paths.server_log.exists() else self.paths.logs
        self._open_in_explorer(target)

    @staticmethod
    def _open_in_explorer(path: Path) -> None:
        """Open a file or folder in Windows Explorer (or the OS default handler).

        ``os.startfile`` is the canonical Windows API for this; on non-Windows
        hosts (dev mode) we fall back to ``subprocess.run`` with ``xdg-open`` /
        ``open`` so this module remains importable for syntax-checking.
        """
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
            return
        except AttributeError:
            pass  # not on Windows
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.run([opener, str(path)], check=False)
        except Exception:
            log.exception("failed to open %s", path)

    def _on_settings(self, _icon, _item=None) -> None:
        """Spawn the Settings window as a subprocess.

        pywebview owns its own main thread and cannot share one with pystray;
        running it inline deadlocks both. The subprocess strategy also keeps
        the tray responsive while Settings is open and isolates any GUI
        crash from the supervisor.

        We reuse the bundled interpreter so we don't depend on a system
        Python being on PATH.
        """
        # Reuse an existing window if it's still alive.
        if self._settings_proc and self._settings_proc.poll() is None:
            return

        python = self.paths.bundled_python
        if not python.exists():
            # Dev fallback: use whatever interpreter is running us.
            python = Path(sys.executable)

        # Use ``packaging.app_main_win.settings_window`` so its ``from .``
        # imports resolve. PYTHONPATH points at the repo / install root so
        # the ``packaging`` package is importable.
        env = os.environ.copy()
        repo_root = self.paths.install_root
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{repo_root}{os.pathsep}{existing}" if existing else str(repo_root)
        )

        try:
            self._settings_proc = subprocess.Popen(
                [str(python), "-m", "packaging.app_main_win.settings_window"],
                env=env,
                cwd=str(self.paths.app_support),
            )
        except Exception:
            log.exception("failed to launch Settings window")
            return

        # Watch the subprocess so we can restart the supervisor if the user
        # asked Save & Restart. The subprocess writes ``.env`` directly and
        # exits with code 3 to request a restart, mirroring the macOS
        # in-process flow.
        threading.Thread(
            target=self._watch_settings, daemon=True, name="settings-watcher"
        ).start()

    def _watch_settings(self) -> None:
        proc = self._settings_proc
        if proc is None:
            return
        try:
            rc = proc.wait()
        except Exception:
            log.exception("settings_proc wait failed")
            return
        if rc == 3:
            log.info("Settings requested restart; respawning uvicorn")
            threading.Thread(target=self.supervisor.restart, daemon=True).start()
        elif rc == 4:
            # "Save without restart" — the supervisor doesn't need a kick,
            # but if it's stopped (first-run flow), start it now.
            if self.supervisor.state in (ServerState.STOPPED, ServerState.CRASHED):
                threading.Thread(target=self.supervisor.start, daemon=True).start()

    def _on_about(self, _icon, _item) -> None:
        # No native About panel on Windows; fall back to opening brand.json
        # or just logging. (V1: keep it minimal.)
        data = paths_mod.load_brand_json()
        log.info(
            "About %s: version=%s",
            self.paths.app_name,
            data.get("version", "0.0.0"),
        )

    def _on_quit(self, _icon, _item) -> None:
        try:
            self.supervisor.shutdown()
        except Exception:
            log.exception("supervisor.shutdown raised")
        try:
            if self._settings_proc and self._settings_proc.poll() is None:
                self._settings_proc.terminate()
        except Exception:
            log.exception("failed to terminate settings_proc")
        self.icon.stop()


def main() -> int:
    p = paths_mod.ensure_dirs()
    logfile = p.logs / "tray.log"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(logfile, mode="a", encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    log.info("app=%s slug=%s install_root=%s", p.app_name, p.slug, p.install_root)

    # Make the packaged kimi_cli importable so seed_branding can reach
    # ``kimi_cli.web.db.crud`` for the SQLite seed step.
    if p.kimi_cli.exists():
        sys.path.insert(0, str(p.kimi_cli.parent))

    app = OpenKimoApp(p)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
