"""Menu-bar entry point for the OpenKimo .app.

Loaded by the C launcher (`Contents/MacOS/<AppName>`) which calls
``Py_BytesMain`` with this package directory as ``argv[1]``.

Responsibilities:
  • bootstrap user-writable Python overlay (pip.conf + bin/ shims)
  • seed Web UI branding into SQLite on first run
  • supervise the uvicorn child process
  • render menu-bar status + menu items
  • own the native Settings window
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import rumps

from . import dotenv_io, paths as paths_mod, seed_branding, userenv
from .server import ServerState, UvicornSupervisor

log = logging.getLogger(__name__)


# ---------- icon helpers --------------------------------------------------

_ICON_GLYPHS = {
    ServerState.RUNNING: "●",
    ServerState.STARTING: "◐",
    ServerState.STOPPED: "○",
    ServerState.CRASHED: "✗",
}


def _status_line(state: ServerState, port: int) -> str:
    glyph = _ICON_GLYPHS.get(state, "?")
    if state == ServerState.RUNNING:
        return f"{glyph} Running · port {port}"
    if state == ServerState.STARTING:
        return f"{glyph} Starting…"
    if state == ServerState.CRASHED:
        return f"{glyph} Crashed (see logs)"
    return f"{glyph} Stopped"


# ---------- terminal helpers ---------------------------------------------

def _write_terminal_init(p: paths_mod.AppPaths) -> Path:
    """Materialise a one-shot init script for ``Open Terminal Here``."""
    script = p.app_support / "shell-init.command"
    script.write_text(
        "#!/bin/bash\n"
        f'cd "{p.work_dir}"\n'
        f'export PYTHONUSERBASE="{p.userbase}"\n'
        f'export PIP_CONFIG_FILE="{p.pip_conf}"\n'
        f'export PATH="{p.userbase_bin}:{p.bundled_python_bin}:$PATH"\n'
        f'echo "▶ {p.app_name} Python env active. python: $(python --version)"\n'
        'exec "$SHELL" -i\n'
    )
    script.chmod(0o755)
    return script


# ---------- the menu-bar app ---------------------------------------------

class OpenKimoApp(rumps.App):
    def __init__(self, p: paths_mod.AppPaths):
        super().__init__(p.app_name, title=p.app_name, quit_button=None)
        self.paths = p
        self.supervisor = UvicornSupervisor(p, on_state_change=self._on_state_change)
        self._build_menu()

        # Run blocking work on a worker thread so the Cocoa main loop is free.
        self._worker = threading.Thread(target=self._startup, name="kimi-startup", daemon=True)
        self._worker.start()

    # ---- menu construction --------------------------------------------

    def _build_menu(self) -> None:
        self.status_item = rumps.MenuItem(_status_line(ServerState.STOPPED, self.supervisor.port))
        self.status_item.set_callback(None)  # non-clickable

        self.open_web_item = rumps.MenuItem("Open Web UI", callback=self._on_open_web, key="o")
        self.open_admin_item = rumps.MenuItem("Open Admin", callback=self._on_open_admin)

        # Start/Stop share one slot so they are mutually exclusive.
        self.toggle_item = rumps.MenuItem("Start Server", callback=self._on_toggle)
        self.restart_item = rumps.MenuItem("Restart Server", callback=self._on_restart)

        self.settings_item = rumps.MenuItem("Settings…", callback=self._on_settings, key=",")
        self.config_item = rumps.MenuItem("Open Config Folder", callback=self._on_open_config)
        self.logs_item = rumps.MenuItem("View Logs", callback=self._on_view_logs)

        self.install_item = rumps.MenuItem("Install Package…", callback=self._on_install_pkg)
        self.terminal_item = rumps.MenuItem("Open Terminal Here", callback=self._on_open_terminal)

        self.about_item = rumps.MenuItem(f"About {self.paths.app_name}", callback=self._on_about)
        self.quit_item = rumps.MenuItem(f"Quit {self.paths.app_name}", callback=self._on_quit, key="q")

        self.menu = [
            self.status_item,
            None,
            self.open_web_item,
            self.open_admin_item,
            None,
            self.toggle_item,
            self.restart_item,
            None,
            self.settings_item,
            self.config_item,
            self.logs_item,
            None,
            self.install_item,
            self.terminal_item,
            None,
            self.about_item,
            self.quit_item,
        ]
        self._refresh_menu_state(ServerState.STOPPED)

    def _set_status_title(self, text: str, state: ServerState) -> None:
        """Set the (non-clickable) status row title with state-appropriate color.

        rumps' MenuItem.title only takes a plain string; for color we drop down
        to NSAttributedString and call setAttributedTitle_ on the underlying
        NSMenuItem.
        """
        try:
            from AppKit import (
                NSColor,
                NSForegroundColorAttributeName,
                NSAttributedString,
            )
        except Exception:
            self.status_item.title = text
            return

        color_for = {
            ServerState.RUNNING: NSColor.systemGreenColor(),
            ServerState.STARTING: NSColor.systemOrangeColor(),
            ServerState.CRASHED: NSColor.systemRedColor(),
        }
        color = color_for.get(state)
        try:
            if color is None:
                self.status_item._menuitem.setAttributedTitle_(None)
                self.status_item.title = text
                return
            attrs = {NSForegroundColorAttributeName: color}
            attr_str = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            self.status_item._menuitem.setAttributedTitle_(attr_str)
        except Exception:
            self.status_item.title = text

    def _refresh_menu_state(self, state: ServerState) -> None:
        running = state == ServerState.RUNNING
        starting = state == ServerState.STARTING
        stopped = state in (ServerState.STOPPED, ServerState.CRASHED)

        # rumps MenuItem doesn't expose enabled directly; use NSMenuItem.
        def _enable(item: rumps.MenuItem, on: bool) -> None:
            try:
                item._menuitem.setEnabled_(bool(on))
            except Exception:
                pass

        if stopped:
            self.toggle_item.title = "Start Server"
            self.toggle_item.set_callback(self._on_toggle)
        else:
            self.toggle_item.title = "Stop Server"
            self.toggle_item.set_callback(self._on_toggle)

        _enable(self.open_web_item, running)
        _enable(self.open_admin_item, running)
        _enable(self.toggle_item, True)  # always clickable; title indicates action
        _enable(self.restart_item, running or starting)

    # ---- startup ------------------------------------------------------

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
            # Defer to main thread so AppKit is happy.
            rumps.Timer(self._open_settings_first_run, 0.1).start()
            return
        self.supervisor.start()

    def _needs_setup(self) -> bool:
        env = dotenv_io.read_env(self.paths.env_file)
        return not any(env.get(k) for k in ("KIMI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"))

    def _open_settings_first_run(self, timer: rumps.Timer) -> None:
        timer.stop()
        self._on_settings(None)

    # ---- supervisor callback ------------------------------------------

    def _on_state_change(self, state: ServerState, info: dict) -> None:
        port = info.get("port", self.supervisor.port)

        def update():
            self._set_status_title(_status_line(state, port), state)
            self._refresh_menu_state(state)
            if state == ServerState.RUNNING:
                self._maybe_open_browser(port)

        # AppKit/rumps require UI updates on the main thread. The supervisor's
        # watcher runs in a worker thread without an NSRunLoop, so NSTimer
        # would never fire there. Hop to the main queue instead.
        from Foundation import NSOperationQueue
        NSOperationQueue.mainQueue().addOperationWithBlock_(update)

    _opened_once = False

    def _maybe_open_browser(self, port: int) -> None:
        if self._opened_once:
            return
        self._opened_once = True
        try:
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:
            log.exception("webbrowser.open failed")

    # ---- menu handlers ------------------------------------------------

    def _on_open_web(self, _):
        webbrowser.open(f"http://127.0.0.1:{self.supervisor.port}")

    def _on_open_admin(self, _):
        webbrowser.open(f"http://127.0.0.1:{self.supervisor.port}/admin")

    def _on_toggle(self, _):
        if self.supervisor.state in (ServerState.RUNNING, ServerState.STARTING):
            threading.Thread(target=self.supervisor.stop, daemon=True).start()
        else:
            threading.Thread(target=self.supervisor.start, daemon=True).start()

    def _on_restart(self, _):
        threading.Thread(target=self.supervisor.restart, daemon=True).start()

    def _on_open_config(self, _):
        self.paths.app_support.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(self.paths.app_support)], check=False)

    def _on_view_logs(self, _):
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        if self.paths.server_log.exists():
            subprocess.run(["open", str(self.paths.server_log)], check=False)
        else:
            subprocess.run(["open", str(self.paths.logs)], check=False)

    def _on_open_terminal(self, _):
        try:
            script = _write_terminal_init(self.paths)
            subprocess.run(["open", "-a", "Terminal", str(script)], check=False)
        except Exception as e:
            log.exception("open terminal failed")
            rumps.alert("Open Terminal Here", str(e))

    def _on_install_pkg(self, _):
        win = rumps.Window(
            title="Install Package",
            message=f"Installs into the {self.paths.app_name} user overlay.\n"
                    f"Equivalent to: pip install --user <args>",
            default_text="",
            ok="Install",
            cancel="Cancel",
            dimensions=(360, 22),
        )
        resp = win.run()
        if not resp.clicked or not resp.text.strip():
            return
        try:
            args = shlex.split(resp.text)
        except ValueError as e:
            rumps.alert("Install Package", f"Invalid args: {e}")
            return
        threading.Thread(target=self._run_pip_install, args=(args,), daemon=True).start()

    def _run_pip_install(self, args: list[str]) -> None:
        env = os.environ.copy()
        env.update(userenv.env_overlay(self.paths))
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        cmd = [str(self.paths.bundled_python), "-m", "pip", "install", *args]
        log.info("pip install: %s", cmd)
        try:
            with self.paths.pip_log.open("ab", buffering=0) as fp:
                rc = subprocess.call(cmd, env=env, stdout=fp, stderr=subprocess.STDOUT)
        except Exception as e:
            log.exception("pip install crashed")
            rumps.notification(self.paths.app_name, "Install failed", str(e))
            return
        if rc == 0:
            rumps.notification(
                self.paths.app_name,
                "Package installed",
                f"{' '.join(args)} — available immediately, no restart needed.",
            )
        else:
            rumps.notification(
                self.paths.app_name,
                "Install failed",
                f"pip exit {rc}; see {self.paths.pip_log}",
            )

    def _on_settings(self, _):
        # Imported lazily; PyObjC Cocoa imports only when actually needed.
        try:
            from . import settings_window
            if not hasattr(self, "_settings_ctrl"):
                self._settings_ctrl = settings_window.build_controller(
                    self.paths, on_save=self._on_settings_saved
                )
            self._settings_ctrl.show()
        except Exception as e:
            log.exception("Settings window failed to open")
            rumps.alert(
                title=f"{self.paths.app_name} Settings",
                message=f"{type(e).__name__}: {e}\n\n"
                        f"See {self.paths.logs / 'menubar.log'} for full traceback.",
            )

    def _on_settings_saved(self, restart: bool) -> None:
        if restart:
            threading.Thread(target=self.supervisor.restart, daemon=True).start()

    def _on_about(self, _):
        try:
            from AppKit import NSApp, NSAttributedString
            from Foundation import NSURL
            opts = {
                "ApplicationName": self.paths.app_name,
                "ApplicationVersion": _read_version(self.paths),
                "Copyright": _read_copyright(self.paths),
            }
            credits_path = self.paths.resources / "Credits.rtf"
            if credits_path.exists():
                rtf = NSAttributedString.alloc().initWithRTF_documentAttributes_(
                    open(credits_path, "rb").read(), None
                )
                if rtf is not None:
                    opts["Credits"] = rtf[0] if isinstance(rtf, tuple) else rtf
            NSApp.orderFrontStandardAboutPanelWithOptions_(opts)
            NSApp.activateIgnoringOtherApps_(True)
        except Exception:
            log.exception("About panel failed")
            rumps.alert(f"About {self.paths.app_name}", _read_version(self.paths))

    def _on_quit(self, _):
        try:
            self.supervisor.shutdown()
        finally:
            rumps.quit_application()


def _read_version(p: paths_mod.AppPaths) -> str:
    data = paths_mod.load_brand_json()
    return data.get("version") or "0.0.0"


def _read_copyright(p: paths_mod.AppPaths) -> str:
    data = paths_mod.load_brand_json()
    return data.get("copyright") or "© OpenKimo Contributors. Apache License 2.0."


def _install_edit_menu(app_name: str) -> None:
    """Menu-bar (accessory) apps don't ship with an app/Edit menu, so Cmd+C/V/X
    don't reach NSTextField first responders inside our Settings window. Wire
    up a minimal main menu so the standard editing shortcuts work."""
    try:
        from AppKit import NSApplication, NSMenu, NSMenuItem
    except Exception:
        log.exception("failed to import AppKit for main menu")
        return

    # Force NSApp into existence; rumps doesn't create it until .run().
    app = NSApplication.sharedApplication()

    main_menu = NSMenu.alloc().init()

    # 1) App menu — system reads the title from the first item's submenu title.
    app_item = NSMenuItem.alloc().init()
    main_menu.addItem_(app_item)
    app_menu = NSMenu.alloc().initWithTitle_(app_name)
    app_menu.addItemWithTitle_action_keyEquivalent_(
        f"Hide {app_name}", "hide:", "h"
    )
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItemWithTitle_action_keyEquivalent_(
        f"Quit {app_name}", "terminate:", "q"
    )
    app_item.setSubmenu_(app_menu)

    # 2) Edit menu — Cut/Copy/Paste/Select All/Undo/Redo via responder chain.
    edit_item = NSMenuItem.alloc().init()
    main_menu.addItem_(edit_item)
    edit_menu = NSMenu.alloc().initWithTitle_("Edit")
    edit_menu.addItemWithTitle_action_keyEquivalent_("Undo", "undo:", "z")
    edit_menu.addItemWithTitle_action_keyEquivalent_("Redo", "redo:", "Z")
    edit_menu.addItem_(NSMenuItem.separatorItem())
    edit_menu.addItemWithTitle_action_keyEquivalent_("Cut", "cut:", "x")
    edit_menu.addItemWithTitle_action_keyEquivalent_("Copy", "copy:", "c")
    edit_menu.addItemWithTitle_action_keyEquivalent_("Paste", "paste:", "v")
    edit_menu.addItem_(NSMenuItem.separatorItem())
    edit_menu.addItemWithTitle_action_keyEquivalent_("Select All", "selectAll:", "a")
    edit_item.setSubmenu_(edit_menu)

    app.setMainMenu_(main_menu)


def main() -> int:
    p = paths_mod.ensure_dirs()
    # Tee logs to a file so menu-bar process crashes are inspectable even when
    # launched from Finder/Launchpad (where stderr is discarded).
    logfile = p.logs / "menubar.log"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(logfile, mode="a"))
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    log.info("app=%s slug=%s bundle=%s", p.app_name, p.slug, p.bundle)

    # Make our packaged kimi_cli importable inside the menu-bar process too,
    # so seed_branding can import the SQLite CRUD helpers.
    if p.kimi_cli.exists():
        sys.path.insert(0, str(p.kimi_cli.parent))

    app = OpenKimoApp(p)
    _install_edit_menu(p.app_name)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
