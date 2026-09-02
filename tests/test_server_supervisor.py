"""Behavioural tests for the uvicorn supervisor's exit handling.

`packaging/` had no importable test coverage at all — every existing reference
to it reads the source as text — so a race here was invisible to the suite.
These drive `UvicornSupervisor` directly with a fake child process; nothing
spawns a real uvicorn.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packaging.app_main import server as srv  # noqa: E402


class FakeProc:
    """Just the two attributes the supervisor touches on a child."""

    def __init__(self, rc: int | None = None) -> None:
        self._rc = rc
        self.pid = 4242

    def poll(self) -> int | None:
        return self._rc


def _supervisor() -> srv.UvicornSupervisor:
    """A supervisor with its collaborators stubbed, built without __init__."""
    s = object.__new__(srv.UvicornSupervisor)
    s._lock = threading.RLock()
    s._proc = None
    s._explicit_stop = False
    s._stop_supervisor = threading.Event()
    s._restart_attempts = 0
    s._state = srv.ServerState.RUNNING
    s.spawns: list[int] = []
    s.states: list[srv.ServerState] = []
    s._spawn_locked = lambda: s.spawns.append(1)
    s._set_state = lambda st, **kw: s.states.append(st)
    return s


class TestRespawnRespectsAStopRequest:
    """The crash path sleeps up to 10s before respawning; stop() runs meanwhile.

    Sampling `_explicit_stop` before that sleep and acting on the sample after
    it is a check-then-act race: the user picked "Stop Server", the menu
    flipped to "Start Server", and the watcher spawned a fresh uvicorn two
    seconds later. On the shutdown() path it left an orphaned
    start_new_session child holding the port after the app had quit.
    """

    def test_a_stop_during_backoff_cancels_the_respawn(self) -> None:
        sup = _supervisor()
        proc = FakeProc(rc=1)
        sup._proc = proc

        def stop_midway() -> None:
            time.sleep(0.3)
            with sup._lock:
                sup._explicit_stop = True

        threading.Thread(target=stop_midway).start()
        sup._handle_exit(rc=1, proc=proc)

        assert sup.spawns == [], "a cancelled stop must not leave a live server"

    def test_shutdown_during_backoff_also_cancels_it(self) -> None:
        sup = _supervisor()
        proc = FakeProc(rc=1)
        sup._proc = proc

        def shutdown_midway() -> None:
            time.sleep(0.3)
            sup._stop_supervisor.set()

        threading.Thread(target=shutdown_midway).start()
        sup._handle_exit(rc=1, proc=proc)

        assert sup.spawns == [], "quitting must not spawn an orphan holding the port"


class TestASupersededChildIsIgnored:
    """restart() terminates the old child and spawns a new one in one call.

    The watcher then reports the old child's rc. Clearing `_proc`
    unconditionally dropped the reference to the *live* replacement: the
    supervisor lost track of it, spawned again, found the port held by the
    child it had just orphaned, and reported CRASHED / port_in_use while an
    unsupervised uvicorn kept serving.
    """

    def test_a_late_exit_does_not_clear_the_live_child(self) -> None:
        sup = _supervisor()
        old, live = FakeProc(rc=-15), FakeProc()
        sup._proc = live

        sup._handle_exit(rc=-15, proc=old)

        assert sup._proc is live
        assert sup.spawns == []

    def test_the_current_child_is_still_handled(self) -> None:
        sup = _supervisor()
        proc = FakeProc(rc=0)
        sup._proc = proc
        sup._explicit_stop = True

        sup._handle_exit(rc=0, proc=proc)

        assert sup._proc is None
        assert sup.states[-1] is srv.ServerState.STOPPED


class TestTheWatcherReportsItsOwnDeath:
    """_spawn_locked() can raise, and it did so straight out of the thread.

    FileNotFoundError when app_layer_python is missing (paths.py falls back
    silently when the runtimes glob finds nothing), OSError from mkdir or
    _write_atomic on a full disk. The thread died, and state and menu froze at
    their last value: "Running · port 5494" with no server and no supervision.
    """

    def test_a_raising_respawn_marks_crashed_before_propagating(self) -> None:
        sup = _supervisor()
        sup._proc = FakeProc(rc=1)

        def boom() -> None:
            raise FileNotFoundError("app_layer_python")

        sup._spawn_locked = boom

        with pytest.raises(FileNotFoundError):
            sup._watch_loop()

        assert sup.states[-1] is srv.ServerState.CRASHED
        assert sup._proc is None


class TestTheRetryBudgetIsConsecutive:
    """It was only reset in start() and on exit code 3, never on RUNNING.

    So three crashes spread over a week of healthy uptime spent the budget,
    and the fourth unrelated crash went straight to "giving up after 3
    retries" even though every prior recovery had succeeded.
    """

    def test_reaching_running_clears_the_budget(self) -> None:
        import inspect

        source = inspect.getsource(srv.UvicornSupervisor._watch_forever)
        running = source[source.index("_check_health(self._port"):]

        assert "_restart_attempts = 0" in running.split("ServerState.RUNNING")[0], (
            "the budget must be cleared when the child reaches RUNNING"
        )
