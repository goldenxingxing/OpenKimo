from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from kimo_cli.utils.logging import logger
from kimo_cli.utils.subprocess_env import get_clean_env

from .models import TaskControl
from .store import BackgroundTaskStore

_PROCESS_WAIT_TIMEOUT_S = 5.0


def terminate_process_tree_windows(pid: int, *, force: bool) -> None:
    args = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    subprocess.run(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=_PROCESS_WAIT_TIMEOUT_S,
    )


def _signal_process_group_posix(pgid: int, sig: int) -> None:
    """Send ``sig`` to the process group ``pgid``, tolerating a reaped leader.

    ``os.killpg`` returns ``EPERM`` on macOS once the group leader has been
    reaped, even though SIGTERM-ignoring descendants are still alive in the
    (orphaned) group and need the later SIGKILL. Fall back to enumerating the
    surviving members with ``ps`` and signaling each directly so cleanup never
    crashes and still reaches the descendants.
    """
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            out = subprocess.run(
                ["ps", "-o", "pid=", "-g", str(pgid)],
                capture_output=True,
                text=True,
                check=False,
                timeout=_PROCESS_WAIT_TIMEOUT_S,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return
        for token in out.split():
            if token.isdigit():
                try:
                    os.kill(int(token), sig)
                except (ProcessLookupError, PermissionError):
                    continue


async def _cancel_helper_tasks(tasks: tuple[asyncio.Task[Any] | None, ...]) -> None:
    """Cancel helper tasks without replacing the worker's active exception."""
    for task in tasks:
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(
                "Background worker helper task failed during shutdown: {error}", error=exc
            )


async def run_background_task_worker(
    task_dir: Path,
    *,
    heartbeat_interval_ms: int = 5000,
    control_poll_interval_ms: int = 500,
    kill_grace_period_ms: int = 2000,
) -> None:
    task_dir = task_dir.expanduser().resolve()
    task_id = task_dir.name
    store = BackgroundTaskStore(task_dir.parent)
    spec = store.read_spec(task_id)
    runtime = store.read_runtime(task_id)

    runtime.status = "starting"
    runtime.worker_pid = os.getpid()
    runtime.started_at = time.time()
    runtime.heartbeat_at = runtime.started_at
    runtime.updated_at = runtime.started_at
    store.write_runtime(task_id, runtime)

    control = store.read_control(task_id)
    if control.kill_requested_at is not None:
        runtime.status = "killed"
        runtime.interrupted = True
        runtime.finished_at = time.time()
        runtime.updated_at = runtime.finished_at
        runtime.failure_reason = control.kill_reason or "Killed before command start"
        store.write_runtime(task_id, runtime)
        return

    if spec.command is None or spec.shell_path is None or spec.cwd is None:
        runtime.status = "failed"
        runtime.finished_at = time.time()
        runtime.updated_at = runtime.finished_at
        runtime.failure_reason = "Task spec is incomplete for bash worker"
        store.write_runtime(task_id, runtime)
        return

    process: asyncio.subprocess.Process | None = None
    process_wait_task: asyncio.Task[int] | None = None
    control_task: asyncio.Task[None] | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    stop_event = asyncio.Event()
    kill_sent_at: float | None = None
    timed_out = False
    timeout_reason: str | None = None

    async def _heartbeat_loop() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(heartbeat_interval_ms / 1000)
            current = store.read_runtime(task_id)
            if current.finished_at is not None:
                return
            current.heartbeat_at = time.time()
            current.updated_at = current.heartbeat_at
            # Heartbeats repeat for the whole task lifetime; skip fsync and keep
            # slow filesystem operations off the process-supervision loop.
            await asyncio.to_thread(store.write_runtime, task_id, current, fsync=False)

    async def _signal_process_tree(force: bool = False) -> None:
        nonlocal kill_sent_at
        if process is None:
            return
        kill_sent_at = kill_sent_at or time.monotonic()

        if os.name == "nt":
            await asyncio.to_thread(terminate_process_tree_windows, process.pid, force=force)
            return

        # Signal the process group even when the leader has already exited:
        # SIGTERM-ignoring descendants still need the later SIGKILL.
        _signal_process_group_posix(process.pid, signal.SIGKILL if force else signal.SIGTERM)

    async def _wait_for_process_bounded() -> None:
        if process is None or process.returncode is not None:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=_PROCESS_WAIT_TIMEOUT_S)
        except TimeoutError:
            logger.error(
                "Timed out waiting for background process {pid} after SIGKILL",
                pid=process.pid,
            )

    async def _cleanup_process(*, force: bool = False) -> None:
        if process is None:
            return
        await _signal_process_tree(force=force)
        if not force:
            await asyncio.sleep(kill_grace_period_ms / 1000)
        await _signal_process_tree(force=True)
        await _wait_for_process_bounded()

    async def _control_loop() -> None:
        while not stop_event.is_set():
            current_control: TaskControl = store.read_control(task_id)
            if current_control.kill_requested_at is not None:
                await _signal_process_tree(force=current_control.force)
                if (
                    kill_sent_at is not None
                    and time.monotonic() - kill_sent_at >= kill_grace_period_ms / 1000
                ):
                    await _signal_process_tree(force=True)
            await asyncio.sleep(control_poll_interval_ms / 1000)

    def _raise_helper_failure(task: asyncio.Task[None], name: str) -> None:
        if not task.done():
            return
        exc = task.exception()
        if exc is not None:
            raise exc
        raise RuntimeError(f"Background worker {name} helper stopped unexpectedly")

    try:
        output_path = store.output_path(task_id)
        with output_path.open("ab") as output_file:
            env = get_clean_env()
            # Override SHELL so commands that read $SHELL see the bash we're
            # actually running, mirroring the foreground Shell tool's behavior.
            env["SHELL"] = spec.shell_path
            spawn_kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": output_file,
                "stderr": output_file,
                "cwd": spec.cwd,
                "env": env,
            }
            if os.name == "nt":
                spawn_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                spawn_kwargs["start_new_session"] = True

            # Enable pipefail for bash so a pipeline's exit code reflects the
            # failing stage (``python x.py | tail`` must not report tail's 0).
            command = (
                f"set -o pipefail; {spec.command}" if spec.shell_name == "bash" else spec.command
            )
            args = (spec.shell_path, "-c", command)
            process = await asyncio.create_subprocess_exec(*args, **spawn_kwargs)

            runtime = store.read_runtime(task_id)
            runtime.status = "running"
            runtime.child_pid = process.pid
            runtime.child_pgid = process.pid if os.name != "nt" else None
            runtime.updated_at = time.time()
            runtime.heartbeat_at = runtime.updated_at
            store.write_runtime(task_id, runtime)

            process_wait_task = asyncio.create_task(process.wait())
            heartbeat_task = asyncio.create_task(_heartbeat_loop())
            control_task = asyncio.create_task(_control_loop())
            supervised = (process_wait_task, heartbeat_task, control_task)
            done, _ = await asyncio.wait(
                supervised,
                timeout=spec.timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                timed_out = True
                timeout_reason = f"Command timed out after {spec.timeout_s}s"
                await _cleanup_process()
                returncode = process.returncode
            else:
                # A failed heartbeat/control helper is as important as process
                # completion: never leave the child unsupervised.
                _raise_helper_failure(heartbeat_task, "heartbeat")
                _raise_helper_failure(control_task, "control")
                if process_wait_task not in done:
                    raise RuntimeError("Background worker supervisor stopped without a process")
                returncode = await process_wait_task

            # The leader can exit between a kill request and helper escalation.
            # Reap its remaining process group before publishing a terminal state.
            control = store.read_control(task_id)
            if control.kill_requested_at is not None:
                await _cleanup_process(force=control.force)
                returncode = process.returncode
    except asyncio.CancelledError:
        await _cleanup_process()
        runtime = store.read_runtime(task_id)
        runtime.status = "killed"
        runtime.interrupted = True
        runtime.finished_at = time.time()
        runtime.updated_at = runtime.finished_at
        runtime.heartbeat_at = runtime.finished_at
        runtime.exit_code = process.returncode if process is not None else None
        runtime.failure_reason = "Worker cancelled"
        store.write_runtime(task_id, runtime)
        raise
    except Exception as exc:
        await _cleanup_process(force=True)
        logger.exception("Background task worker failed")
        runtime = store.read_runtime(task_id)
        runtime.status = "failed"
        runtime.interrupted = True
        runtime.finished_at = time.time()
        runtime.updated_at = runtime.finished_at
        runtime.failure_reason = str(exc)
        store.write_runtime(task_id, runtime)
        return
    finally:
        stop_event.set()
        await _cancel_helper_tasks((heartbeat_task, control_task))
        if process_wait_task is not None and not process_wait_task.done():
            process_wait_task.cancel()
            await _cancel_helper_tasks((process_wait_task,))

    runtime = store.read_runtime(task_id)
    control = store.read_control(task_id)
    runtime.finished_at = time.time()
    runtime.updated_at = runtime.finished_at
    runtime.exit_code = returncode
    runtime.heartbeat_at = runtime.finished_at
    if timed_out:
        runtime.status = "failed"
        runtime.interrupted = True
        runtime.timed_out = True
        runtime.failure_reason = timeout_reason
    elif control.kill_requested_at is not None:
        runtime.status = "killed"
        runtime.interrupted = True
        runtime.failure_reason = control.kill_reason or "Killed"
    elif returncode == 0:
        runtime.status = "completed"
        runtime.failure_reason = None
    else:
        runtime.status = "failed"
        runtime.failure_reason = f"Command failed with exit code {returncode}"
    store.write_runtime(task_id, runtime)
