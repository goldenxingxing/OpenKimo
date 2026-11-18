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

import atexit
import contextlib
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import rumps

from . import dotenv_io, runtime_repair, seed_branding, userenv
from . import paths as paths_mod
from .server import ServerState, UvicornSupervisor, request_config_reload

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
    bin_dirs = shlex.quote(str(p.userbase_bin) + ":" + str(p.bundled_python_bin))
    greeting = shlex.quote("▶ " + p.app_name + " Python env active.")
    script.write_text(
        "#!/bin/bash\n"
        f"cd {shlex.quote(str(p.work_dir))}\n"
        f"export PYTHONUSERBASE={shlex.quote(str(p.userbase))}\n"
        f"export PIP_CONFIG_FILE={shlex.quote(str(p.pip_conf))}\n"
        f'export PATH={bin_dirs}:"$PATH"\n'
        f'echo {greeting} "python: $(python --version)"\n'
        'exec "$SHELL" -i\n'
    )
    script.chmod(0o755)
    return script


# ---------- the menu-bar app ---------------------------------------------

class OpenKimoApp(rumps.App):
    def __init__(self, p: paths_mod.AppPaths):
        # Show only the brand glyph in the menu bar; fall back to the app name
        # as text if the packaged icon is somehow missing. (If the bar item
        # disappears entirely on notch-equipped Macs, that's macOS overflow —
        # close other menu-bar apps or drag the item left of the notch.)
        icon_path = str(p.menubar_icon) if p.menubar_icon.exists() else None
        kwargs: dict = {"quit_button": None}
        if icon_path:
            kwargs["icon"] = icon_path
            # A *Template.png is monochrome: macOS tints it for a light or
            # dark menu bar, as it does the system icons.
            kwargs["template"] = p.menubar_icon.name.endswith("Template.png")
        else:
            kwargs["title"] = p.app_name
        super().__init__(p.app_name, **kwargs)
        self.paths = p
        self.supervisor = UvicornSupervisor(p, on_state_change=self._on_state_change)
        self._build_menu()

        # Run blocking work on a worker thread so the Cocoa main loop is free.
        self._worker = threading.Thread(target=self._startup, name="kimo-startup", daemon=True)
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
                NSAttributedString,
                NSColor,
                NSForegroundColorAttributeName,
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
        #
        # setEnabled_ alone does nothing: NSMenu.autoenablesItems defaults to
        # YES and rumps never clears it, so AppKit re-validates every item just
        # before the menu is displayed and overrides whatever we set for any
        # item whose target responds to the action. The result was a "Open Web
        # UI" that stayed clickable with the server stopped and opened a tab to
        # a dead 127.0.0.1:5494. Turning autoenables off makes setEnabled_ the
        # authority.
        try:
            self.menu._menu.setAutoenablesItems_(False)
        except Exception:
            log.debug("could not disable menu auto-enabling", exc_info=True)

        def _enable(item: rumps.MenuItem, on: bool) -> None:
            with contextlib.suppress(Exception):
                item._menuitem.setEnabled_(bool(on))

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
            # Hop to the main queue, not rumps.Timer. _startup() runs on the
            # kimo-startup worker thread, and rumps.Timer.start() adds the
            # NSTimer to NSRunLoop.currentRunLoop() — that thread's run loop,
            # which is never run and dies with the daemon thread. The callback
            # never fired, and because this branch also returns before
            # supervisor.start(), a fresh install sat at "Stopped" with no
            # Settings window and no server. Same hazard, same fix as
            # _on_state_change below.
            from Foundation import NSOperationQueue
            NSOperationQueue.mainQueue().addOperationWithBlock_(
                self._open_settings_first_run
            )
            return
        self.supervisor.start()

    def _needs_setup(self) -> bool:
        env = dotenv_io.read_env(self.paths.env_file)
        # New multi-provider config takes precedence: any non-empty LLM_PROVIDERS
        # blob is treated as "configured", regardless of legacy keys.
        if (env.get("LLM_PROVIDERS") or "").strip():
            return False
        return not any(env.get(k) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"))

    def _open_settings_first_run(self) -> None:
        """Open Settings on first run. Called on the main queue, not a timer."""
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

            ctrl = getattr(self, "_settings_ctrl", None)
            window = ctrl.window() if ctrl is not None else None
            if window is not None and window.isVisible():
                # Already on screen: bring it forward rather than stack a
                # second copy of the same window.
                ctrl.show()
                return
            # Otherwise rebuild it. The controller snapshots .env and
            # config.toml when it is built, so a cached one keeps showing what
            # was true the first time Settings was opened this launch — and a
            # Save writes that stale snapshot back, including initial_default,
            # which reverts a model the user picked in the web UI meanwhile.
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
        """Apply what was saved. Restart only when the user asked for one.

        The reload path is why Settings no longer has to end a session to
        change a setting: the running server re-reads what changed, applies it
        to the sessions that are idle, and hands it to the busy ones when their
        prompt finishes.
        """
        if restart:
            threading.Thread(target=self.supervisor.restart, daemon=True).start()
            return
        # A server that is not running cannot re-read anything. The first-run
        # path returns before supervisor.start(), so the very first Save had
        # nothing to hand the settings to: it wrote a reload signal into a file
        # nobody was polling and the menu bar sat at "Stopped" forever. Windows
        # already starts the server here.
        if self.supervisor.state in (ServerState.STOPPED, ServerState.CRASHED):
            threading.Thread(target=self.supervisor.start, daemon=True).start()
            return
        request_config_reload(self.paths)

    def _on_about(self, _):
        try:
            from AppKit import NSApp, NSAttributedString
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
        # shutdown() can block for GRACEFUL_KILL_TIMEOUT (5s) plus a further 2s
        # after kill() when the child ignores SIGTERM — a request stuck past
        # uvicorn's --timeout-graceful-shutdown, say. This runs on the AppKit
        # main thread, so that showed up as a frozen menu bar and a spinner for
        # up to 7 seconds. Every other supervisor control here is already
        # pushed onto a worker thread; do the same and quit when it returns.
        def _shutdown_then_quit() -> None:
            try:
                self.supervisor.shutdown()
            except Exception:
                log.exception("shutdown failed during quit")
            finally:
                from Foundation import NSOperationQueue
                NSOperationQueue.mainQueue().addOperationWithBlock_(
                    rumps.quit_application
                )

        threading.Thread(
            target=_shutdown_then_quit, name="kimo-quit", daemon=True
        ).start()


def _install_shutdown_backstops(supervisor: UvicornSupervisor) -> None:
    """Make the uvicorn child die with the menu-bar process on every exit path.

    The rumps Quit item routes through ``_on_quit``, but external quits —
    Dock right-click Quit, ``osascript -e 'quit app'``, the AppleEvent a
    launcher or updater sends — hit ``terminate:`` directly and used to
    leave the ``start_new_session`` child orphaned on the port. rumps fires
    ``before_quit`` from ``applicationWillTerminate:``, which runs on every
    termination path; atexit and SIGTERM cover interpreter-level exits.
    All three funnel into ``UvicornSupervisor.shutdown()``, which is
    idempotent, so the menu path that already shut down is a cheap no-op.
    """

    def _quiet_shutdown() -> None:
        try:
            supervisor.shutdown()
        except Exception:
            log.exception("shutdown backstop raised")

    atexit.register(_quiet_shutdown)
    try:
        rumps.events.before_quit.register(_quiet_shutdown)
    except Exception:
        log.exception("failed to register before_quit backstop")

    def _sigterm(signum: int, _frame) -> None:
        _quiet_shutdown()
        # The supervisor is down and there is nothing else worth running;
        # leave without unwinding the AppKit run loop.
        os._exit(128 + signum)

    signal.signal(signal.SIGTERM, _sigterm)


def _read_version(_p: paths_mod.AppPaths) -> str:
    """Version string from brand.json.

    The parameter is unused and kept only for call-site symmetry: the value
    comes from paths_mod.load_brand_json(), which resolves through the
    module-level lru_cached app_paths(), so passing a different AppPaths here
    would not select a different brand file.
    """
    data = paths_mod.load_brand_json()
    return data.get("version") or "0.0.0"


def _read_copyright(_p: paths_mod.AppPaths) -> str:
    """Copyright string from brand.json. See _read_version on the parameter."""
    data = paths_mod.load_brand_json()
    return data.get("copyright") or "© OpenKimo Contributors. Apache License 2.0."


# PyObjC needs a strong reference to the menu item target for the whole app
# lifetime; a local target would be collected as soon as this function returns.
_QUIT_TARGET: object | None = None


def _install_edit_menu(app_name: str, quit_callback=None) -> None:
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
    # NSApplication's terminate: bypasses OpenKimoApp._on_quit entirely, so
    # the supervisor shutdown never runs and the child worker (started with
    # start_new_session) is left orphaned, still holding the port. Route the
    # menu item through the same callback as the rumps quit button.
    if quit_callback is not None:
        from Foundation import NSObject

        class _QuitTarget(NSObject):
            def quitViaSupervisor_(self, _sender):
                quit_callback()

        global _QUIT_TARGET
        _QUIT_TARGET = _QuitTarget.alloc().init()
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"Quit {app_name}", "quitViaSupervisor:", "q"
        )
        quit_item.setTarget_(_QUIT_TARGET)
        app_menu.addItem_(quit_item)
    else:
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
    with contextlib.suppress(Exception):
        handlers.append(logging.FileHandler(logfile, mode="a"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    log.info("app=%s slug=%s bundle=%s", p.app_name, p.slug, p.bundle)

    # Heal venvstacks configs baked with the build machine's paths, before the
    # uvicorn server (and its worker subprocesses) start the bundled Python.
    runtime_repair.repair_layer_configs(p.bundled_python_root.parent)

    # Make our packaged kimo_cli importable inside the menu-bar process too,
    # so seed_branding can import the SQLite CRUD helpers.
    if p.kimo_cli.exists():
        sys.path.insert(0, str(p.kimo_cli.parent))

    app = OpenKimoApp(p)
    _install_shutdown_backstops(app.supervisor)
    _install_edit_menu(p.app_name, lambda: app._on_quit(None))
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
