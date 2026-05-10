"""Visual regression harness for the macOS Settings window.

Usage (must be invoked with the bundled app-layer Python so PyObjC is
present):

    /path/to/OpenKimo.app/Contents/Resources/runtimes/app-app-openkimo/bin/python \\
        scripts/screenshot-settings.py

Writes a PNG to /tmp/settings-screenshot.png. Designed to be run again after
any layout change in `packaging/app_main/settings_window.py`.

The harness:
  1. Adds the bundle's Resources/ to sys.path so `app_main.*` imports.
  2. Stands up an NSApplication with a regular activation policy.
  3. Builds an AppPaths instance that points at a temporary Application
     Support / Logs root (so the harness does not pollute real state).
  4. Constructs the Settings controller and shows the window.
  5. Spins the run loop briefly to let auto-layout settle, then captures
     the window via `screencapture -x -l <window_id>`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _bundle_root() -> Path:
    """Resolve the .app bundle that this Python lives inside."""
    here = Path(sys.executable).resolve()
    for parent in here.parents:
        if parent.suffix == ".app":
            return parent
    raise SystemExit(
        "screenshot-settings.py must be run with the bundled app-layer "
        "Python (the one inside OpenKimo.app/Contents/Resources/runtimes/)"
    )


BUNDLE = _bundle_root()
RESOURCES = BUNDLE / "Contents" / "Resources"

# Make `app_main.*` and (transitively) `kimi_cli.*` importable.
sys.path.insert(0, str(RESOURCES))

# Frameworks layer site-packages, in case `app_main.*` pulls in anything
# that lives there (e.g. pydantic via dotenv_io's neighbours).
_framework_site = RESOURCES / "runtimes" / "framework-framework-kimi" / "lib" / "python3.12" / "site-packages"
if _framework_site.is_dir():
    sys.path.insert(0, str(_framework_site))

from AppKit import NSApplication, NSApp  # noqa: E402
from Foundation import NSDate, NSRunLoop  # noqa: E402

from app_main import settings_window  # noqa: E402
from app_main.paths import AppPaths  # noqa: E402


def _make_paths() -> AppPaths:
    """Build a self-contained AppPaths under a temporary Application Support."""
    tmp_root = Path(tempfile.mkdtemp(prefix="openkimo-screenshot-"))
    app_support = tmp_root / "AppSupport"
    logs = tmp_root / "Logs"
    userbase = app_support / "python-userbase"
    for d in (app_support, logs, userbase, userbase / "bin"):
        d.mkdir(parents=True, exist_ok=True)
    env_file = app_support / ".env"
    env_file.touch(exist_ok=True)  # so dotenv_io.read_editable returns {}

    py_root = RESOURCES / "runtimes" / "cpython-3.12"
    app_layer_root = next(
        (p for p in (RESOURCES / "runtimes").glob("app-*") if p.is_dir()),
        py_root,
    )

    return AppPaths(
        bundle=BUNDLE,
        contents=BUNDLE / "Contents",
        resources=RESOURCES,
        macos=BUNDLE / "Contents" / "MacOS",
        frameworks=BUNDLE / "Contents" / "Frameworks",
        bundled_python_root=py_root,
        bundled_python_bin=py_root / "bin",
        bundled_python=py_root / "bin" / "python3",
        app_layer_python=app_layer_root / "bin" / "python",
        app_main=RESOURCES / "app_main",
        kimi_cli=RESOURCES / "kimi_cli",
        static_dir=RESOURCES / "kimi_cli" / "web" / "static",
        brand_json=RESOURCES / "brand.json",
        menubar_icon=RESOURCES / "MenuBarIcon.png",
        app_support=app_support,
        env_file=env_file,
        pip_conf=app_support / "pip.conf",
        userbase=userbase,
        userbase_bin=userbase / "bin",
        work_dir=app_support / "work",
        sessions_dir=app_support / "sessions",
        output_dir=app_support / "output",
        logs=logs,
        server_log=logs / "server.log",
        pip_log=logs / "pip.log",
        app_name="OpenKimo",
        slug="openkimo",
    )


def _spin(duration: float) -> None:
    """Pump the run loop for `duration` seconds so layout settles."""
    deadline = NSDate.dateWithTimeIntervalSinceNow_(duration)
    NSRunLoop.currentRunLoop().runUntilDate_(deadline)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--env-fixture",
        type=str,
        default=None,
        help="Path to a .env fixture to copy into AppPaths.env_file before"
             " building the Settings controller.",
    )
    ap.add_argument(
        "--out",
        type=str,
        default="/tmp/settings-screenshot.png",
        help="Destination PNG path (default: /tmp/settings-screenshot.png).",
    )
    args = ap.parse_args()
    out_path = args.out

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(0)  # NSApplicationActivationPolicyRegular

    paths = _make_paths()
    if args.env_fixture:
        src = Path(args.env_fixture)
        if not src.is_file():
            print(f"fixture not found: {src}", file=sys.stderr)
            return 4
        shutil.copyfile(src, paths.env_file)
    controller = settings_window.build_controller(paths, on_save=lambda r: None)
    controller.show()

    # First spin: fire initial layout pass.
    _spin(0.6)
    # Second spin after explicit layout request, in case the first frame
    # was still in the "intrinsic only" state.
    win = controller.window()
    win.layoutIfNeeded()
    win.contentView().layoutSubtreeIfNeeded()
    _spin(0.8)

    # Scroll the embedded NSScrollView (if any) back to the TOP so the
    # screenshot consistently captures the LLM section header. AppKit's
    # default behaviour is to leave the document offset wherever it ended
    # up after the layout pass — for tall content that's the bottom.
    try:
        from AppKit import NSScrollView
        def _walk(view):
            yield view
            for sub in view.subviews():
                yield from _walk(sub)
        for v in _walk(win.contentView()):
            if isinstance(v, NSScrollView):
                doc = v.documentView()
                if doc is not None:
                    # In an unflipped NSView, the top of the document is at
                    # y == frame.height. Scroll so that point is visible.
                    h = float(doc.frame().size.height)
                    from Foundation import NSMakeRect
                    doc.scrollRectToVisible_(NSMakeRect(0.0, h - 1.0, 1.0, 1.0))
                break
        _spin(0.2)
    except Exception:
        pass

    window_id = int(win.windowNumber())
    if window_id <= 0:
        print(f"window number invalid ({window_id}); window may not be on screen", file=sys.stderr)
        return 2

    if os.path.exists(out_path):
        os.unlink(out_path)
    rc = subprocess.run(
        ["screencapture", "-x", "-o", "-l", str(window_id), out_path],
        check=False,
    ).returncode

    if rc != 0:
        print(f"screencapture exited with {rc}", file=sys.stderr)
        return rc
    if not os.path.exists(out_path):
        print(f"screencapture did not produce {out_path}", file=sys.stderr)
        return 3

    print(out_path)
    return 0


if __name__ == "__main__":
    rc = main()
    # Tear down before exiting so we don't leave a window hanging if invoked
    # interactively. NSApp.terminate_ is not strictly necessary because we
    # never called .run() — Python exit handles it cleanly.
    sys.exit(rc)
