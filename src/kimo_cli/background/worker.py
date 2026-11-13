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


def terminate_process_tree_windows(pid: int, *, force: bool) -> None:
    args = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    subprocess.run(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


async def _cancel_helper_tasks(tasks: tuple[asyncio.Task[Any] | None, ...]) -> None:
    """Cancel the helper tasks and swallow whatever they failed with.

    Awaiting a task that already failed re-raises its exception, which would
    replace the error the worker was handling — or skip the terminal state
    written after the cleanup.
    """
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
            # Heartbeats repeat for the whole task lifetime; skip the fsync so a
            # slow or stalled disk cannot freeze this event loop (and starve the
            # very heartbeat that proves the worker is alive).
            store.write_runtime(task_id, current, fsync=False)

    async def _terminate_process(force: bool = False) -> None:
        nonlocal kill_sent_at
        if process is None or process.returncode is not None:
            return
        kill_sent_at = kill_sent_at or time.time()

        try:
            if os.name == "nt":
                await asyncio.to_thread(terminate_process_tree_windows, process.pid, force=force)
                return

            target_pgid = process.pid
            if force:
                os.killpg(target_pgid, signal.SIGKILL)
            else:
                os.killpg(target_pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    async def _control_loop() -> None:
        nonlocal kill_sent_at
        while not stop_event.is_set():
            await asyncio.sleep(control_poll_interval_ms / 1000)
            current_control: TaskControl = store.read_control(task_id)
            if current_control.kill_requested_at is not None:
                await _terminate_process(force=current_control.force)
                if (
                    kill_sent_at is not None
                    and process is not None
                    and process.returncode is None
                    and time.time() - kill_sent_at >= kill_grace_period_ms / 1000
                ):
                    await _terminate_process(force=True)

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
            last_known_runtime = runtime

            heartbeat_task = asyncio.create_task(_heartbeat_loop())
            control_task = asyncio.create_task(_control_loop())
            if spec.timeout_s is None:
                returncode = await process.wait()
            else:
                try:
                    returncode = await asyncio.wait_for(process.wait(), timeout=spec.timeout_s)
                except TimeoutError:
                    timed_out = True
                    timeout_reason = f"Command timed out after {spec.timeout_s}s"
                    await _terminate_process(force=False)
                    try:
                        returncode = await asyncio.wait_for(
                            process.wait(),
                            timeout=kill_grace_period_ms / 1000,
                        )
                    except TimeoutError:
                        await _terminate_process(force=True)
                        returncode = await process.wait()
    except Exception as exc:
        await _terminate_process(force=True)
        logger.exception("Background task worker failed")
        runtime = store.read_runtime(task_id)
        runtime.status = "failed"
        runtime.finished_at = time.time()
        runtime.updated_at = runtime.finished_at
        runtime.failure_reason = str(exc)
        store.write_runtime(task_id, runtime)
        return
    finally:
        stop_event.set()
        await _cancel_helper_tasks((heartbeat_task, control_task))

    runtime = last_known_runtime.model_copy()
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
