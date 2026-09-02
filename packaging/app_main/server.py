"""uvicorn supervisor.

Spawns `python -m uvicorn kimi_cli.web.app:create_app --factory ...` as a
subprocess of the menu-bar app and watches its lifecycle.

Exit code protocol:
  0 / SIGTERM    user-initiated stop, do not auto-restart
  3              "config changed, please restart" (Settings → Save & Restart)
  other          crash; up to MAX_AUTO_RESTARTS attempts before giving up
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Callable

from . import dotenv_io, userenv
from .paths import AppPaths

log = logging.getLogger(__name__)

MAX_AUTO_RESTARTS = 3
GRACEFUL_KILL_TIMEOUT = 5.0
HEALTHCHECK_DEADLINE = 30.0
EXIT_CODE_RESTART = 3

# Windows graceful-shutdown plumbing. On POSIX the supervisor uses
# `start_new_session=True` so the uvicorn child has its own process group
# and signals don't fan out to siblings. On Windows that kwarg is a no-op;
# the equivalent is the CREATE_NEW_PROCESS_GROUP creation flag, which lets
# us send CTRL_BREAK_EVENT to the child (and only the child) at shutdown.
_IS_WINDOWS = sys.platform == "win32"


def _popen_new_group_kwargs(platform: str) -> dict:
    if platform == "win32":
        new_group = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
        )
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        return {"creationflags": new_group | no_window}
    return {"start_new_session": True}


_POPEN_NEW_GROUP_KWARGS = _popen_new_group_kwargs(sys.platform)

# Uvicorn ships a default LOGGING_CONFIG without timestamps. Inject one with
# ISO-style timestamps in both default and access formatters; written to a
# file under app_support and passed via --log-config so uvicorn picks it up
# in its child workers too.
_UVICORN_LOG_CONFIG: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(asctime)s %(levelprefix)s %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
            "use_colors": False,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            "datefmt": "%Y-%m-%d %H:%M:%S",
            "use_colors": False,
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}


class ServerState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    CRASHED = "crashed"


def _build_env(p: AppPaths) -> dict[str, str]:
    """Assemble the environment for the uvicorn subprocess."""
    env = os.environ.copy()

    # 1. Local mode flags (mirrors scripts/start.sh local_mode).
    env["KIMI_USE_CONTAINERS"] = "false"
    env["ENABLE_BROWSER"] = "false"
    env["ENABLE_JUPYTER"] = "false"

    # 2. Static frontend bundled inside the .app.
    if p.static_dir.exists():
        env["KIMI_WEB_STATIC_DIR"] = str(p.static_dir)

    # 3. User-mutable storage paths.
    # The identity the agent presents in conversations follows the app brand
    # (a user .env may still override it below).
    # A desktop install is one person on loopback. The server-side default of
    # a 24-hour session means a login page every morning for no security worth
    # having; a month of sliding window is the honest trade here. Set before
    # the user's .env is layered on, so anyone who disagrees can override it.
    env["KIMI_WEB_SESSION_MAX_AGE"] = str(30 * 24 * 60 * 60)

    env["KIMI_AGENT_NAME"] = p.app_name
    env["KIMI_DEFAULT_WORK_DIR"] = str(p.work_dir)
    env["KIMI_SESSIONS_DIR"] = str(p.sessions_dir)
    env["KIMI_SHARE_DIR"] = str(p.sessions_dir)
    env["KIMI_OUTPUT_DIR"] = str(p.output_dir)
    env["OPENKIMO_SKILL_DIR"] = str(p.skill_dir)
    env["OPENKIMO_APP_DATA_DIR"] = str(p.app_support)
    env["OPENKIMO_WIKI_ROOT"] = str(p.wiki_dir)

    # 3b. Our own stdio is UTF-8, whatever the host codepage says: the worker's
    # stderr is read back with .decode("utf-8"), and a model response containing
    # a character the ANSI codepage lacks would otherwise raise
    # UnicodeEncodeError far from anything the user did.
    #
    # PYTHONIOENCODING only covers this process's own std streams. Do NOT add
    # PYTHONUTF8=1 here: that also switches locale.getpreferredencoding(), which
    # is what subprocess(text=True) decodes child output with — and the Windows
    # tools we shell out to legitimately emit the ANSI codepage. It shipped in
    # v0.1.29 and broke git-bash detection on a Chinese Windows, where
    # `where.exe git` prints GBK and the decode died inside subprocess's reader
    # thread, taking the whole worker with it.
    env["PYTHONIOENCODING"] = "utf-8"

    # 4. User-overlay Python (so kimi-cli's shell tool can pip install).
    env.update(userenv.env_overlay(p))

    # 5. PYTHONPATH so the bundled kimi_cli source is importable.
    py_paths = [str(p.kimi_cli.parent)] if p.kimi_cli.exists() else []
    if py_paths:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(py_paths + ([existing] if existing else []))

    # 6. Layer the user's .env on top (Settings-managed values).
    user_env = dotenv_io.read_env(p.env_file)
    env.update(user_env)  # user's choices win over our defaults
    # …except for desktop-owned mode and storage paths, which must always win.
    # Legacy .env files may still contain the removed Paths settings.
    env["KIMI_USE_CONTAINERS"] = "false"
    env["ENABLE_BROWSER"] = "false"
    env["ENABLE_JUPYTER"] = "false"
    env["KIMI_DEFAULT_WORK_DIR"] = str(p.work_dir)
    env["KIMI_SESSIONS_DIR"] = str(p.sessions_dir)
    env["KIMI_SHARE_DIR"] = str(p.sessions_dir)
    env["KIMI_SESSION_DATA_DIR"] = str(p.sessions_dir)
    env["KIMI_OUTPUT_DIR"] = str(p.output_dir)
    env["OPENKIMO_SKILL_DIR"] = str(p.skill_dir)
    env["OPENKIMO_APP_DATA_DIR"] = str(p.app_support)
    env["OPENKIMO_WIKI_ROOT"] = str(p.wiki_dir)

    # 7. HF_HOME from the optional Paths setting.
    hf = user_env.get("HF_CACHE_HOST_PATH") or env.get("HF_CACHE_HOST_PATH", "")
    if hf:
        env["HF_HOME"] = hf

    return env


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file + rename so no reader ever sees a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _resolve_port(env: dict[str, str]) -> int:
    raw = env.get("KIMI_WEB_PORT", "5494")
    try:
        return int(raw)
    except ValueError:
        return 5494


#: The server's health endpoint. Note the ``z``: a probe of ``/health`` hits
#: the SPA catch-all, which answers *any* GET with index.html and 200 — so it
#: reported every port as healthy, including one held by a stranger.
_HEALTH_PATH = "/healthz"


def _probe_health(port: int) -> tuple[bool, int | None]:
    """Ask whoever is on ``port`` whether they are our healthy server.

    Returns ``(healthy, pid)``. Healthy means the response was JSON that says
    so — an HTML page with a 200 is not a health check. ``pid`` is None when
    the responder does not report one (a server older than this field).
    """
    try:
        import json as _json
        import urllib.request
        with urllib.request.urlopen(  # noqa: S310 - localhost only
            f"http://127.0.0.1:{port}{_HEALTH_PATH}", timeout=1.0
        ) as resp:
            if not 200 <= resp.status < 300:
                return False, None
            body = _json.loads(resp.read(4096).decode("utf-8"))
    except Exception:
        return False, None

    if not isinstance(body, dict) or body.get("status") != "ok":
        return False, None
    raw = body.get("pid")
    pid = int(raw) if isinstance(raw, (int, str)) and str(raw).isdigit() else None
    return True, pid


def _port_is_listening(port: int) -> bool:
    """Whether anything at all accepts connections on ``port``."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _check_health(port: int, expect_pid: int | None = None) -> bool:
    """Whether *our* server is serving ``port``.

    The pid check is the point. Asking the port alone means a second copy of
    the app — or any leftover server — answers on behalf of a child that has
    already died on a bind error: the supervisor marks it RUNNING, the restart
    counter resets, and it respawns forever without ever saying why. A
    reported pid that is not ours proves the port belongs to someone else; no
    pid at all (older server) keeps the previous lenient behaviour.
    """
    healthy, pid = _probe_health(port)
    if not healthy:
        return False
    if expect_pid is not None and pid is not None and pid != expect_pid:
        return False
    return True


def _port_owner_pid(port: int) -> int | None:
    """Pid of a healthy server already on ``port``, if it reports one."""
    healthy, pid = _probe_health(port)
    return pid if healthy else None


def _reap_orphan_listener_windows(port: int) -> None:
    """Windows counterpart of :func:`_reap_orphan_listener`.

    The POSIX version shells out to ``lsof`` and ``ps``, neither of which
    exists on Windows — so on Windows it silently did nothing, and a leftover
    uvicorn from a force-killed tray app kept the port forever while the new
    supervisor respawned a doomed child every two seconds.

    Ownership is decided the same way and just as narrowly: the command line
    must mention ``kimi_cli.web.app``. There is no orphan test — Windows does
    not reparent to a known pid — so this only ever kills our own server, and
    the single-instance mutex is what keeps a *live* sibling from being here
    in the first place.
    """
    try:
        netstat = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, timeout=3.0,
        ).stdout
    except Exception:
        log.exception("netstat failed; cannot reap orphan listener")
        return

    pids: set[int] = set()
    for line in netstat.splitlines():
        parts = line.split()
        # PROTO  LOCAL          FOREIGN     STATE       PID
        if len(parts) < 5 or parts[3].upper() != "LISTENING":
            continue
        local = parts[1]
        if not local.rsplit(":", 1)[-1] == str(port):
            continue
        try:
            pids.add(int(parts[4]))
        except ValueError:
            continue

    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            cmd = subprocess.run(
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine",
                ],
                capture_output=True, text=True, timeout=5.0,
            ).stdout.strip()
        except Exception:
            log.exception("could not read command line of pid %d", pid)
            continue
        if "kimi_cli.web.app" not in cmd:
            log.warning("port %d held by foreign pid %d (%s); leaving alone", port, pid, cmd)
            continue
        log.warning("reaping orphan uvicorn pid %d on port %d", pid, port)
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=5.0,
            )
        except Exception:
            log.exception("failed to kill orphan pid %d", pid)
            continue
        # taskkill returns before the socket is released; give it a moment so
        # the bind that follows does not race it.
        for _ in range(20):
            if _port_owner_pid(port) is None:
                break
            time.sleep(0.1)


def _reap_orphan_listener(port: int) -> None:
    """Kill orphan kimi_cli.web.app listeners on `port`.

    A prior force-killed menu-bar process leaves its uvicorn child running
    with PPID=1 (reparented to launchd). Without this, the new supervisor
    can't bind the port and burns through its retries. We only target
    processes whose cmdline mentions kimi_cli.web.app AND whose parent is
    launchd — never a dev server whose parent is a shell.
    """
    if sys.platform == "win32":
        _reap_orphan_listener_windows(port)
        return
    try:
        lsof = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=2.0,
        )
    except Exception:
        return
    for pid_str in lsof.stdout.split():
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        try:
            ps = subprocess.run(
                ["ps", "-o", "ppid=,command=", "-p", str(pid)],
                capture_output=True, text=True, timeout=1.0,
            ).stdout.strip()
        except Exception:
            continue
        if not ps:
            continue
        ppid_str, _, cmd = ps.partition(" ")
        try:
            ppid = int(ppid_str)
        except ValueError:
            continue
        if "kimi_cli.web.app" not in cmd:
            log.warning("port %d held by foreign pid %d (%s); leaving alone",
                        port, pid, cmd)
            continue
        if ppid != 1:
            log.warning("port %d held by pid %d (parent %d, not orphaned); leaving alone",
                        port, pid, ppid)
            continue
        log.warning("reaping orphan uvicorn pid %d on port %d", pid, port)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except Exception:
            log.exception("failed to terminate orphan pid %d", pid)
            continue
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
            time.sleep(0.2)


class UvicornSupervisor:
    """Owns the uvicorn child process; thread-safe controls."""

    def __init__(
        self,
        paths: AppPaths,
        on_state_change: Callable[[ServerState, dict], None] | None = None,
    ):
        self.paths = paths
        self.on_state_change = on_state_change or (lambda *_: None)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._state = ServerState.STOPPED
        self._port = 5494
        self._restart_attempts = 0
        self._explicit_stop = False
        self._stop_supervisor = threading.Event()
        self._watcher: threading.Thread | None = None
        self._healthcheck_deadline = 0.0

    # ---- public API -----------------------------------------------------

    @property
    def state(self) -> ServerState:
        return self._state

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return
            self._explicit_stop = False
            self._restart_attempts = 0
            self._spawn_locked()

    def stop(self) -> None:
        with self._lock:
            self._explicit_stop = True
            self._terminate_locked()
        self._set_state(ServerState.STOPPED)

    def restart(self) -> None:
        self.stop()
        self.start()

    def request_restart_via_exit_code(self) -> None:
        """Used by Settings → Save & Restart. The supervisor watcher will
        see exit code 3 and respawn automatically."""
        with self._lock:
            self._explicit_stop = False
            if self._proc and self._proc.poll() is None:
                self._send_graceful_signal(self._proc)

    def shutdown(self) -> None:
        """Final teardown on app quit."""
        self._stop_supervisor.set()
        self.stop()

    # ---- internals ------------------------------------------------------

    def _set_state(self, new: ServerState, **extra) -> None:
        if self._state == new:
            return
        self._state = new
        try:
            self.on_state_change(new, {"port": self._port, **extra})
        except Exception:
            log.exception("on_state_change handler raised")

    def _spawn_locked(self) -> None:
        env = _build_env(self.paths)
        self._port = _resolve_port(env)
        host = env.get("KIMI_WEB_HOST", "127.0.0.1")

        _reap_orphan_listener(self._port)

        # If the port is still taken after reaping, spawning is pointless: the
        # child would die on a bind error and be restarted forever, with the
        # UI showing nothing but lost sockets. Stop and say what is wrong.
        # Asked as "is anything listening", not "is a healthy server there" —
        # an unrelated program on the port blocks the bind just as effectively.
        if _port_is_listening(self._port):
            owner = _port_owner_pid(self._port)
            log.error("port %s is already taken (owner pid %s); not spawning", self._port, owner)
            self._set_state(
                ServerState.CRASHED,
                exit_code=None,
                reason="port_in_use",
                port_owner_pid=owner,
            )
            return

        self.paths.logs.mkdir(parents=True, exist_ok=True)
        log_fp = self.paths.server_log.open("ab", buffering=0)

        # Written on every spawn, so two instances racing here can leave a
        # reader seeing a truncated file — uvicorn then dies parsing its own
        # log config. Swap it in atomically instead.
        log_config_path = self.paths.app_support / "uvicorn-log.json"
        _write_atomic(log_config_path, json.dumps(_UVICORN_LOG_CONFIG))

        cmd = [
            str(self.paths.app_layer_python),
            "-m", "uvicorn",
            "kimi_cli.web.app:create_app",
            "--factory",
            "--host", host,
            "--port", str(self._port),
            "--log-level", "info",
            "--log-config", str(log_config_path),
            "--timeout-graceful-shutdown", "3",
        ]
        log.info("spawning uvicorn: %s", " ".join(cmd))
        self._proc = subprocess.Popen(  # noqa: S603 - args are well-known
            cmd,
            env=env,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            cwd=str(self.paths.app_support),
            **_POPEN_NEW_GROUP_KWARGS,
        )
        self._healthcheck_deadline = time.monotonic() + HEALTHCHECK_DEADLINE
        self._set_state(ServerState.STARTING)
        self._ensure_watcher()

    def _terminate_locked(self) -> None:
        if not self._proc:
            return
        if self._proc.poll() is not None:
            self._proc = None
            return
        try:
            self._send_graceful_signal(self._proc)
            self._proc.wait(timeout=GRACEFUL_KILL_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Forced kill: terminate() on POSIX is SIGTERM (already sent), so
            # escalate to kill() (SIGKILL); on Windows terminate() is itself
            # TerminateProcess() — hard kill — which is the right escalation
            # after CTRL_BREAK_EVENT was ignored.
            if _IS_WINDOWS:
                self._proc.terminate()
            else:
                self._proc.kill()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        finally:
            self._proc = None

    @staticmethod
    def _send_graceful_signal(proc: subprocess.Popen) -> None:
        """Politely ask the child to shut down.

        POSIX: SIGTERM via Popen.terminate() — unchanged from prior behavior.
        Windows: CTRL_BREAK_EVENT, which uvicorn handles like SIGINT and
        which only reaches the child because we spawned it with
        CREATE_NEW_PROCESS_GROUP.
        """
        if _IS_WINDOWS:
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                # Process already gone or signal not supported on this build —
                # fall back to TerminateProcess so we don't leave it running.
                proc.terminate()
        else:
            proc.terminate()

    def _ensure_watcher(self) -> None:
        if self._watcher and self._watcher.is_alive():
            return
        t = threading.Thread(target=self._watch_loop, name="srv-watcher", daemon=True)
        self._watcher = t
        t.start()

    def _watch_loop(self) -> None:
        """Supervise the child until shutdown.

        Wrapped: _handle_exit() respawns, and _spawn_locked() can raise —
        FileNotFoundError when app_layer_python is missing (paths.py falls
        back silently when the runtimes glob finds nothing), OSError from
        mkdir/_write_atomic on a full disk. Those propagated out of this
        thread and killed it, leaving state and menu frozen at their last
        value: the menu bar kept showing "Running · port 5494" with no server
        and no supervision, indefinitely.
        """
        try:
            self._watch_forever()
        except BaseException:
            log.exception("watcher thread died; marking the server crashed")
            self._proc = None
            self._set_state(
                ServerState.CRASHED, exit_code=None, reason="supervisor_error"
            )
            raise

    def _watch_forever(self) -> None:
        while not self._stop_supervisor.is_set():
            with self._lock:
                proc = self._proc
                state = self._state
            if proc is None:
                # CRASHED is a conclusion, not a transient: something decided
                # it and attached a reason the user is meant to read. "No child
                # process" is true of it as much as of a clean stop, so without
                # this the watcher would overwrite the diagnosis with "Stopped"
                # half a second later.
                if state not in (ServerState.STOPPED, ServerState.CRASHED):
                    self._set_state(ServerState.STOPPED)
                time.sleep(0.5)
                if self._stop_supervisor.is_set():
                    return
                continue

            rc = proc.poll()
            if rc is None:
                # Still alive — health-check only while STARTING.
                if state == ServerState.STARTING:
                    if _check_health(self._port, expect_pid=proc.pid):
                        # Reaching RUNNING clears the budget: it was only ever
                        # reset in start() and on exit code 3, so three crashes
                        # spread over a week of healthy uptime spent it, and
                        # the fourth unrelated crash went straight to
                        # "giving up after 3 retries".
                        self._restart_attempts = 0
                        self._set_state(ServerState.RUNNING)
                    elif time.monotonic() > self._healthcheck_deadline:
                        # Took too long; treat as crashed.
                        with self._lock:
                            self._terminate_locked()
                        self._handle_exit(rc=-1, proc=proc)
                        continue
                time.sleep(1.0)
                continue

            # Child exited.
            self._handle_exit(rc=rc, proc=proc)

    def _respawn(self) -> None:
        """Spawn a replacement, unless someone decided to stop meanwhile.

        The crash path sleeps for up to ten seconds before it gets here, and
        stop()/shutdown() run on other threads (the tray callbacks). Sampling
        the flags before that sleep and acting on the sample afterwards is a
        check-then-act race: the user picked "Stop Server", saw the menu flip,
        and the watcher spawned a fresh uvicorn two seconds later. On the
        shutdown() path it left an orphaned start_new_session child holding
        the port after the app had quit.
        """
        with self._lock:
            if self._explicit_stop or self._stop_supervisor.is_set():
                log.info("respawn cancelled; a stop was requested while backing off")
                return
            self._spawn_locked()

    def _handle_exit(self, rc: int, proc: subprocess.Popen | None = None) -> None:
        with self._lock:
            # Only clear the handle if it is still the process that exited.
            # restart() terminates the old child and spawns a new one, and the
            # watcher then reported the old child's rc: clearing
            # unconditionally dropped the reference to the *live* replacement,
            # so the supervisor lost track of it, tried to spawn again, and
            # reported CRASHED / port_in_use against the child it had just
            # orphaned — while an unsupervised uvicorn kept running.
            if proc is None or self._proc is proc:
                self._proc = None
            else:
                log.debug("ignoring exit of a superseded child (rc=%s)", rc)
                return
            explicit = self._explicit_stop
        if explicit:
            self._set_state(ServerState.STOPPED)
            return
        if rc == EXIT_CODE_RESTART:
            log.info("uvicorn requested restart (exit 3); respawning")
            self._restart_attempts = 0
            self._respawn()
            return
        # Crash path.
        if self._restart_attempts < MAX_AUTO_RESTARTS:
            self._restart_attempts += 1
            log.warning("uvicorn exited rc=%s; auto-restart %d/%d",
                        rc, self._restart_attempts, MAX_AUTO_RESTARTS)
            time.sleep(min(2 ** self._restart_attempts, 10))
            self._respawn()
            return
        log.error("uvicorn exited rc=%s; giving up after %d retries",
                  rc, MAX_AUTO_RESTARTS)
        self._set_state(ServerState.CRASHED, exit_code=rc)
