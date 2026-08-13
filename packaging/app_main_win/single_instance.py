"""Refuse to run a second copy of the tray app on Windows.

Two copies cannot share the port: the first one binds it and the second one's
uvicorn dies with WinError 10048 on every attempt, forever. Nothing in the UI
says so — the browser keeps talking to whichever instance won, and a session
that loses its socket just reports "lost connection".

The guard is a named kernel mutex. Windows keeps it alive only while a handle
is open, so a crashed or force-killed instance releases it automatically —
unlike a pidfile, which has to be cleaned up by the process that can no longer
run. The handle is held for the process lifetime on purpose: releasing it would
re-open the window this closes.

Note the scope. This stops a *second tray app*. It does not help when the tray
was force-killed and left its uvicorn child holding the port — no app is
running then, so there is nothing for the mutex to collide with. That case is
what the orphan reaper in ``app_main.server`` is for.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

_ERROR_ALREADY_EXISTS = 183

# Handle kept at module scope: the mutex lives exactly as long as this process.
_handle: int | None = None


def acquire(app_name: str) -> bool:
    """Claim the single-instance mutex. False means another copy owns it.

    Never raises: a machine where the mutex cannot be created (an unusual
    security policy, a non-Windows host) is better served by launching than by
    refusing to start.
    """
    global _handle
    if sys.platform != "win32":
        return True

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE

        # "Local\" scopes the name to this login session, so two different users
        # on the same machine each get their own instance — they have separate
        # app data and separate ports anyway.
        name = f"Local\\{app_name}-tray-singleton"
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            log.warning("CreateMutexW failed (err=%s); starting anyway", ctypes.get_last_error())
            return True
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _handle = handle
        return True
    except Exception:
        log.exception("single-instance check failed; starting anyway")
        return True
