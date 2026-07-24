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
    env["KIMI_DEFAULT_WORK_DIR"] = str(p.work_dir)
    env["KIMI_SESSIONS_DIR"] = str(p.sessions_dir)
    env["KIMI_SHARE_DIR"] = str(p.sessions_dir)
    env["KIMI_OUTPUT_DIR"] = str(p.output_dir)
    env["OPENKIMO_SKILL_DIR"] = str(p.skill_dir)

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

    # 7. HF_HOME from the optional Paths setting.
    hf = user_env.get("HF_CACHE_HOST_PATH") or env.get("HF_CACHE_HOST_PATH", "")
    if hf:
        env["HF_HOME"] = hf

    return env


def _resolve_port(env: dict[str, str]) -> int:
    raw = env.get("KIMI_WEB_PORT", "5494")
    try:
        return int(raw)
    except ValueError:
        return 5494


def _check_health(port: int) -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(  # noqa: S310 - localhost only
            f"http://127.0.0.1:{port}/health", timeout=1.0
        ) as resp:
            return 200 <= resp.status < 500
    except Exception:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            return False


def _reap_orphan_listener(port: int) -> None:
    """Kill orphan kimi_cli.web.app listeners on `port`.

    A prior force-killed menu-bar process leaves its uvicorn child running
    with PPID=1 (reparented to launchd). Without this, the new supervisor
    can't bind the port and burns through its retries. We only target
    processes whose cmdline mentions kimi_cli.web.app AND whose parent is
    launchd — never a dev server whose parent is a shell.
    """
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

        self.paths.logs.mkdir(parents=True, exist_ok=True)
        log_fp = self.paths.server_log.open("ab", buffering=0)

        log_config_path = self.paths.app_support / "uvicorn-log.json"
        log_config_path.write_text(json.dumps(_UVICORN_LOG_CONFIG))

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
        while not self._stop_supervisor.is_set():
            with self._lock:
                proc = self._proc
                state = self._state
            if proc is None:
                if state != ServerState.STOPPED:
                    self._set_state(ServerState.STOPPED)
                time.sleep(0.5)
                if self._stop_supervisor.is_set():
                    return
                continue

            rc = proc.poll()
            if rc is None:
                # Still alive — health-check only while STARTING.
                if state == ServerState.STARTING:
                    if _check_health(self._port):
                        self._set_state(ServerState.RUNNING)
                    elif time.monotonic() > self._healthcheck_deadline:
                        # Took too long; treat as crashed.
                        with self._lock:
                            self._terminate_locked()
                        self._handle_exit(rc=-1)
                        continue
                time.sleep(1.0)
                continue

            # Child exited.
            self._handle_exit(rc=rc)

    def _handle_exit(self, rc: int) -> None:
        with self._lock:
            self._proc = None
            explicit = self._explicit_stop
        if explicit:
            self._set_state(ServerState.STOPPED)
            return
        if rc == EXIT_CODE_RESTART:
            log.info("uvicorn requested restart (exit 3); respawning")
            self._restart_attempts = 0
            with self._lock:
                self._spawn_locked()
            return
        # Crash path.
        if self._restart_attempts < MAX_AUTO_RESTARTS:
            self._restart_attempts += 1
            log.warning("uvicorn exited rc=%s; auto-restart %d/%d",
                        rc, self._restart_attempts, MAX_AUTO_RESTARTS)
            time.sleep(min(2 ** self._restart_attempts, 10))
            with self._lock:
                self._spawn_locked()
            return
        log.error("uvicorn exited rc=%s; giving up after %d retries",
                  rc, MAX_AUTO_RESTARTS)
        self._set_state(ServerState.CRASHED, exit_code=rc)
