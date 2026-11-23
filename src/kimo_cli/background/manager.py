from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kaos.local import local_kaos

from kimo_cli.config import BackgroundConfig
from kimo_cli.notifications import NotificationEvent, NotificationManager
from kimo_cli.session import Session
from kimo_cli.utils.datetime import format_elapsed
from kimo_cli.utils.logging import logger

if TYPE_CHECKING:
    from kimo_cli.soul.agent import Runtime

from .ids import generate_task_id
from .models import (
    TaskOutputChunk,
    TaskRuntime,
    TaskSpec,
    TaskStatus,
    TaskView,
    is_terminal_status,
)
from .store import BackgroundTaskStore

_PID_ALIVE_QUERY_ACCESS = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
_WINDOWS_STILL_ACTIVE = 259


def _reap_unwaited_child(pid: int | None) -> None:
    """Reap a task-worker child that exited without anyone wait()ing on it.

    ``_launch_worker`` intentionally drops the Popen handle, so a worker that
    crashes becomes a zombie and would otherwise stay one for the lifetime of
    this process — making a dead worker look alive to ``os.kill(pid, 0)``.
    """
    if os.name == "nt" or not pid or pid <= 0:
        return
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)


def _pid_exists(pid: int | None) -> bool:
    """Best-effort liveness check for a worker pid (used before declaring a
    stale-heartbeat task "lost", so a stalled-but-alive worker is never
    misjudged)."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is unsupported on Windows; OpenProcess alone cannot
        # distinguish an exited process, so also read its exit code.
        import ctypes
        import ctypes.wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        handle = kernel32.OpenProcess(_PID_ALIVE_QUERY_ACCESS, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == _WINDOWS_STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class BackgroundTaskManager:
    def __init__(
        self,
        session: Session,
        config: BackgroundConfig,
        *,
        notifications: NotificationManager,
        owner_role: str = "root",
    ) -> None:
        self._session = session
        self._config = config
        self._notifications = notifications
        self._owner_role = owner_role
        self._store = BackgroundTaskStore(session.context_file.parent / "tasks")
        self._runtime: Runtime | None = None
        self._live_agent_tasks: dict[str, asyncio.Task[None]] = {}
        self._completion_event: asyncio.Event = asyncio.Event()
        self._checkpoint_delivery_lock: asyncio.Lock = asyncio.Lock()
        # Number of in-flight `wait()` calls (TaskOutput block=true). While
        # > 0 the agent's turn is parked waiting on background task(s) rather
        # than doing substantive work — the wire server uses this to let a
        # new user prompt replace the parked turn instead of queueing.
        self._blocking_waits = 0

    @property
    def completion_event(self) -> asyncio.Event:
        """Event set when a new terminal notification is published.

        Not set immediately when a task becomes terminal — only after
        ``reconcile()`` / ``publish_terminal_notifications()`` runs.
        Deduplicated notifications do not trigger a repeat signal.
        """
        return self._completion_event

    @property
    def store(self) -> BackgroundTaskStore:
        return self._store

    @property
    def role(self) -> str:
        return self._owner_role

    def copy_for_role(self, role: str) -> BackgroundTaskManager:
        manager = BackgroundTaskManager(
            self._session,
            self._config,
            notifications=self._notifications,
            owner_role=role,
        )
        manager._runtime = self._runtime
        return manager

    def bind_runtime(self, runtime: Runtime) -> None:
        self._runtime = runtime

    def _ensure_root(self) -> None:
        if self._owner_role != "root":
            raise RuntimeError("Background tasks are only supported from the root agent.")

    def _ensure_local_backend(self) -> None:
        if self._session.work_dir_meta.kaos != local_kaos.name:
            raise RuntimeError("Background tasks are only supported on local sessions.")

    def _ensure_not_evolution_subprocess(self) -> None:
        """Refuse detached workers inside evolution verify subprocesses.

        A background worker is fully detached (own session via
        ``start_new_session=True``, stdio to DEVNULL), so it outlives the
        verify child's timeout kill and leaks onto the host — observed as
        long-running evaluation scripts still running after verify returned.
        Foreground execution stays inside the verify child's process group
        and is reclaimed by the group kill on timeout.
        """
        if os.environ.get("KIMO_EVOLUTION_SUBPROCESS"):
            raise RuntimeError(
                "Background tasks are disabled in evolution verification "
                "subprocesses (KIMO_EVOLUTION_SUBPROCESS=1): a detached worker "
                "would outlive the verify timeout and leak. Run the command in "
                "the foreground instead."
            )

    def _active_task_count(self) -> int:
        return sum(
            1 for view in self._store.list_views() if not is_terminal_status(view.runtime.status)
        )

    def has_active_tasks(self) -> bool:
        """Return True if any background tasks are in a non-terminal status.

        This includes ``running``, ``awaiting_approval``, and any other
        non-terminal state — not just actively executing tasks.
        """
        return self._active_task_count() > 0

    def _worker_command(self, task_dir: Path) -> list[str]:
        if getattr(sys, "frozen", False):
            return [
                sys.executable,
                "__background-task-worker",
                "--task-dir",
                str(task_dir),
                "--heartbeat-interval-ms",
                str(self._config.worker_heartbeat_interval_ms),
                "--control-poll-interval-ms",
                str(self._config.wait_poll_interval_ms),
                "--kill-grace-period-ms",
                str(self._config.kill_grace_period_ms),
            ]
        return [
            sys.executable,
            "-m",
            "kimo_cli.cli",
            "__background-task-worker",
            "--task-dir",
            str(task_dir),
            "--heartbeat-interval-ms",
            str(self._config.worker_heartbeat_interval_ms),
            "--control-poll-interval-ms",
            str(self._config.wait_poll_interval_ms),
            "--kill-grace-period-ms",
            str(self._config.kill_grace_period_ms),
        ]

    def _launch_worker(self, task_dir: Path) -> int:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": str(task_dir),
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True

        process = subprocess.Popen(self._worker_command(task_dir), **kwargs)
        return process.pid

    def create_bash_task(
        self,
        *,
        command: str,
        description: str,
        timeout_s: int,
        tool_call_id: str,
        shell_name: str,
        shell_path: str,
        cwd: str,
    ) -> TaskView:
        self._ensure_root()
        self._ensure_local_backend()
        self._ensure_not_evolution_subprocess()

        if self._active_task_count() >= self._config.max_running_tasks:
            raise RuntimeError("Too many background tasks are already running.")

        task_id = generate_task_id("bash")
        spec = TaskSpec(
            id=task_id,
            kind="bash",
            session_id=self._session.id,
            description=description,
            tool_call_id=tool_call_id,
            owner_role="root",
            command=command,
            shell_name=shell_name,
            shell_path=shell_path,
            cwd=cwd,
            timeout_s=timeout_s,
        )
        self._store.create_task(spec)
        from kimo_cli.telemetry import track

        track("background_task_created")

        runtime = self._store.read_runtime(task_id)
        task_dir = self._store.task_dir(task_id)
        try:
            worker_pid = self._launch_worker(task_dir)
        except Exception as exc:
            runtime.status = "failed"
            runtime.failure_reason = f"Failed to launch worker: {exc}"
            runtime.finished_at = time.time()
            runtime.updated_at = runtime.finished_at
            self._store.write_runtime(task_id, runtime)
            raise

        runtime = self._store.read_runtime(task_id)
        if runtime.finished_at is None and (
            runtime.status == "created"
            or (runtime.status == "starting" and runtime.worker_pid is None)
        ):
            runtime.status = "starting"
            runtime.worker_pid = worker_pid
            runtime.updated_at = time.time()
            self._store.write_runtime(task_id, runtime)
        return self._store.merged_view(task_id)

    def create_agent_task(
        self,
        *,
        agent_id: str,
        subagent_type: str,
        prompt: str,
        description: str,
        tool_call_id: str,
        model_override: str | None,
        timeout_s: int | None = None,
        resumed: bool = False,
    ) -> TaskView:
        from .agent_runner import BackgroundAgentRunner

        self._ensure_root()
        self._ensure_local_backend()
        self._ensure_not_evolution_subprocess()
        if self._runtime is None:
            raise RuntimeError("Background task manager is not bound to a runtime.")
        if self._active_task_count() >= self._config.max_running_tasks:
            raise RuntimeError("Too many background tasks are already running.")

        task_id = generate_task_id("agent")
        # Explicit None check — the falsy idiom ``timeout_s or default``
        # would silently promote a caller-supplied ``0`` to the agent
        # default, matching the analogous fix in Print's wait-cap reader.
        effective_timeout = (
            timeout_s if timeout_s is not None else self._config.agent_task_timeout_s
        )
        spec = TaskSpec(
            id=task_id,
            kind="agent",
            session_id=self._session.id,
            description=description,
            tool_call_id=tool_call_id,
            owner_role="root",
            # Persist the effective timeout so downstream readers (e.g. the
            # Print-mode ``print_wait_ceiling_s`` cap calculation) can honour
            # an explicit per-agent timeout instead of always falling back to
            # ``config.background.agent_task_timeout_s``.
            timeout_s=effective_timeout,
            kind_payload={
                "agent_id": agent_id,
                "subagent_type": subagent_type,
                "prompt": prompt,
                "model_override": model_override,
                "launch_mode": "background",
            },
        )
        self._store.create_task(spec)
        runtime = self._store.read_runtime(task_id)
        runtime.status = "starting"
        runtime.updated_at = time.time()
        self._store.write_runtime(task_id, runtime)
        task = asyncio.create_task(
            BackgroundAgentRunner(
                runtime=self._runtime,
                manager=self,
                task_id=task_id,
                agent_id=agent_id,
                subagent_type=subagent_type,
                prompt=prompt,
                model_override=model_override,
                timeout_s=effective_timeout,
                resumed=resumed,
            ).run()
        )
        self._live_agent_tasks[task_id] = task

        # Cleanup safety net for the case where the runner is cancelled before
        # its first event-loop step. Python throws CancelledError into a
        # FRAME_CREATED coroutine without executing any of the function body,
        # so the runner's finally block never runs and cannot pop the entry
        # itself. The done callback fires regardless of how the task ends, and
        # is idempotent with the runner's own pop (both use pop(..., None)).
        def _on_runner_done(done: asyncio.Task[None], tid: str = task_id) -> None:
            self._live_agent_tasks.pop(tid, None)
            if done.cancelled():
                return
            exc = done.exception()
            if exc is None:
                return
            logger.error(
                "Background agent task {task_id} failed before startup: {error}",
                task_id=tid,
                error=exc,
            )
            self._mark_task_failed(tid, str(exc))

        task.add_done_callback(_on_runner_done)
        return self._store.merged_view(task_id)

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        limit: int | None = 20,
    ) -> list[TaskView]:
        tasks = self._store.list_views()
        if status is not None:
            tasks = [task for task in tasks if task.runtime.status == status]
        if limit is None:
            return tasks
        return tasks[:limit]

    async def checkpoint_block_for_task(self, task_id: str) -> str:
        """Hand a completed background run's sealed evidence to the root, once.

        Every root delivery path — the notification reconciler and every
        ``TaskOutput`` call — routes through here, so the first caller in a
        receiving turn gets the managed block and all later callers get an
        empty string.  The dedupe key is bound to the sealed run, not to the
        receiving turn, so a replay in a later turn stays suppressed.
        """
        runtime = self._runtime
        if runtime is None or runtime.role != "root":
            return ""
        coordinator = runtime.wiki_coordinator
        if coordinator is None:
            return ""
        async with self._checkpoint_delivery_lock:
            try:
                return await self._deliver_checkpoint_block(task_id, coordinator)
            except Exception:
                logger.warning(
                    "Failed to deliver the Wiki checkpoint for task {task_id}; "
                    "returning output without it",
                    task_id=task_id,
                )
                return ""

    async def _deliver_checkpoint_block(self, task_id: str, coordinator: Any) -> str:
        from kimo_cli.wiki.triggers import canonical_digest

        view = self.get_task(task_id)
        if view is None:
            return ""
        if view.runtime.status != "completed":
            # Failed, killed, lost, timed-out, or still-running runs never
            # produce grounding, and any partial manifest is dropped.
            if is_terminal_status(view.runtime.status):
                await asyncio.to_thread(self._store.delete_wiki_evidence_manifest, task_id)
            return ""
        manifest = await asyncio.to_thread(self._store.read_wiki_evidence_manifest, task_id)
        if manifest is None or not manifest.evidence:
            return ""
        dedupe_key = canonical_digest(
            (
                task_id,
                manifest.agent_id,
                str(manifest.run_generation),
                manifest.summary_hash,
            )
        )
        consumer = view.consumer
        if dedupe_key in consumer.delivered_wiki_checkpoint_keys:
            return ""
        receiving_root_turn_id = coordinator.active_turn_id
        if receiving_root_turn_id is None:
            return ""
        checkpoint = await coordinator.accept_subagent_result(
            agent_id=manifest.agent_id,
            run_generation=manifest.run_generation,
            summary_hash=manifest.summary_hash,
            evidence=manifest.evidence,
            receiving_root_turn_id=receiving_root_turn_id,
            task_id=task_id,
        )
        if checkpoint is None:
            return ""
        block = await coordinator.render_checkpoints((checkpoint,))
        updated = consumer.model_copy(
            update={
                "delivered_wiki_checkpoint_keys": [
                    *consumer.delivered_wiki_checkpoint_keys,
                    dedupe_key,
                ]
            }
        )
        await asyncio.to_thread(self._store.write_consumer, task_id, updated)
        return block

    def get_task(self, task_id: str) -> TaskView | None:
        try:
            return self._store.merged_view(task_id)
        except (FileNotFoundError, ValueError):
            return None

    def resolve_output_path(self, task_id: str) -> Path:
        """Return the canonical output path for *task_id*."""
        return self._store.output_path(task_id)

    def read_output(
        self,
        task_id: str,
        *,
        offset: int = 0,
        max_bytes: int | None = None,
    ) -> TaskOutputChunk:
        view = self._store.merged_view(task_id)
        return self._store.read_output(
            task_id,
            offset,
            max_bytes or self._config.read_max_bytes,
            status=view.runtime.status,
        )

    def tail_output(
        self,
        task_id: str,
        *,
        max_bytes: int | None = None,
        max_lines: int | None = None,
    ) -> str:
        self._store.merged_view(task_id)
        return self._store.tail_output(
            task_id,
            max_bytes=max_bytes or self._config.read_max_bytes,
            max_lines=max_lines or self._config.notification_tail_lines,
        )

    @property
    def has_blocking_wait(self) -> bool:
        """Whether the current turn is parked in a blocking wait on background task(s)."""
        return self._blocking_waits > 0

    async def wait(self, task_id: str, *, timeout_s: int = 30) -> TaskView:
        self._blocking_waits += 1
        try:
            return await self._wait(task_id, timeout_s=timeout_s)
        finally:
            self._blocking_waits -= 1

    async def _wait(self, task_id: str, *, timeout_s: int = 30) -> TaskView:
        end_time = time.monotonic() + timeout_s
        while True:
            view = self._store.merged_view(task_id)
            if is_terminal_status(view.runtime.status):
                return view
            if time.monotonic() >= end_time:
                return view
            await asyncio.sleep(self._config.wait_poll_interval_ms / 1000)

    def _best_effort_kill(self, runtime: TaskRuntime) -> None:
        try:
            if os.name == "nt":
                pid = runtime.child_pid or runtime.worker_pid
                if pid is None:
                    return
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                return

            if runtime.child_pgid is not None:
                os.killpg(runtime.child_pgid, signal.SIGTERM)
                return
            if runtime.child_pid is not None:
                os.kill(runtime.child_pid, signal.SIGTERM)
            elif runtime.worker_pid is not None:
                # The control file remains authoritative; this signal covers the
                # startup window before the detached worker has polled it.
                os.kill(runtime.worker_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            logger.exception("Failed to send best-effort kill signal")

    def kill(self, task_id: str, *, reason: str = "Killed by user") -> TaskView:
        self._ensure_root()
        view = self._store.merged_view(task_id)
        if is_terminal_status(view.runtime.status):
            return view

        if view.spec.kind == "agent":
            self._mark_task_killed(task_id, reason)
            if self._runtime is not None and self._runtime.approval_runtime is not None:
                self._runtime.approval_runtime.cancel_by_source("background_agent", task_id)
            # Keep the task in _live_agent_tasks until BackgroundAgentRunner's
            # finally block removes it. asyncio holds tasks in a WeakSet, so if
            # we drop the only strong reference here the still-pending task can
            # be garbage-collected before cancellation propagates, which fires
            # loop.call_exception_handler with no 'exception' field — surfacing
            # as "Unhandled exception in event loop / Exception None" in the
            # prompt_toolkit terminal.
            task = self._live_agent_tasks.get(task_id)
            if task is not None:
                task.cancel()
            return self._store.merged_view(task_id)

        control = view.control.model_copy(
            update={
                "kill_requested_at": time.time(),
                "kill_reason": reason,
                "force": False,
            }
        )
        self._store.write_control(task_id, control)
        self._best_effort_kill(view.runtime)
        return self._store.merged_view(task_id)

    def kill_all_active(self, *, reason: str = "CLI session ended") -> list[str]:
        """Kill all non-terminal background tasks. Used during CLI shutdown."""
        killed: list[str] = []
        for view in self._store.list_views():
            if is_terminal_status(view.runtime.status):
                continue
            try:
                self.kill(view.spec.id, reason=reason)
                killed.append(view.spec.id)
            except Exception:
                logger.exception(
                    "Failed to kill task {task_id} during shutdown",
                    task_id=view.spec.id,
                )
        return killed

    def recover(self) -> None:
        now = time.time()
        stale_after = self._config.worker_stale_after_ms / 1000
        for view in self._store.list_views():
            if is_terminal_status(view.runtime.status):
                continue
            if view.spec.kind == "agent":
                if view.spec.id in self._live_agent_tasks:
                    continue
                runtime = view.runtime.model_copy()
                runtime.finished_at = now
                runtime.updated_at = now
                runtime.status = "lost"
                runtime.failure_reason = "In-process background agent is no longer running"
                self._store.write_runtime(view.spec.id, runtime)
                agent_id = (view.spec.kind_payload or {}).get("agent_id")
                if (
                    isinstance(agent_id, str)
                    and self._runtime is not None
                    and self._runtime.subagent_store is not None
                ):
                    record = self._runtime.subagent_store.get_instance(agent_id)
                    if record is not None and record.status == "running_background":
                        self._runtime.subagent_store.update_instance(agent_id, status="failed")
                continue
            last_progress_at = (
                view.runtime.heartbeat_at
                or view.runtime.started_at
                or view.runtime.updated_at
                or view.spec.created_at
            )
            if now - last_progress_at <= stale_after:
                continue

            # Re-read runtime to narrow the race window with the worker process.
            fresh_runtime = self._store.read_runtime(view.spec.id)
            if is_terminal_status(fresh_runtime.status):
                continue
            fresh_progress = (
                fresh_runtime.heartbeat_at
                or fresh_runtime.started_at
                or fresh_runtime.updated_at
                or view.spec.created_at
            )
            if now - fresh_progress <= stale_after:
                continue

            # The heartbeat only proves the worker process is scheduling its
            # loop; a stalled-but-alive worker (disk hiccup, heavy load) must
            # not be declared lost. Reap first so a crashed worker doesn't
            # linger as a zombie that looks alive.
            _reap_unwaited_child(fresh_runtime.worker_pid)
            if _pid_exists(fresh_runtime.worker_pid):
                logger.debug(
                    "Background task {task_id} heartbeat stale but worker pid {pid} "
                    "is alive; leaving status as {status}",
                    task_id=view.spec.id,
                    pid=fresh_runtime.worker_pid,
                    status=fresh_runtime.status,
                )
                continue

            runtime = fresh_runtime.model_copy()
            runtime.finished_at = now
            runtime.updated_at = now
            if view.control.kill_requested_at is not None:
                runtime.status = "killed"
                runtime.interrupted = True
                runtime.failure_reason = view.control.kill_reason or "Killed during recovery"
            else:
                runtime.status = "lost"
                runtime.failure_reason = (
                    "Background worker never heartbeat after startup"
                    if fresh_runtime.heartbeat_at is None
                    else "Background worker heartbeat expired"
                )
            self._store.write_runtime(view.spec.id, runtime)

    def reconcile(self, *, limit: int | None = None) -> list[str]:
        self.recover()
        return self.publish_terminal_notifications(limit=limit)

    def publish_terminal_notifications(self, *, limit: int | None = None) -> list[str]:
        published: list[str] = []
        for view in self._store.list_views():
            if not is_terminal_status(view.runtime.status):
                continue

            status = view.runtime.status
            terminal_reason = "timed_out" if view.runtime.timed_out else status
            match terminal_reason:
                case "completed":
                    severity = "success"
                    title = f"Background task completed: {view.spec.description}"
                case "timed_out":
                    severity = "error"
                    title = f"Background task timed out: {view.spec.description}"
                case "failed":
                    severity = "error"
                    title = f"Background task failed: {view.spec.description}"
                case "killed":
                    severity = "warning"
                    title = f"Background task stopped: {view.spec.description}"
                case "lost":
                    severity = "warning"
                    title = f"Background task lost: {view.spec.description}"
                case _:
                    severity = "info"
                    title = f"Background task updated: {view.spec.description}"

            finished_at = view.runtime.finished_at
            started_at = view.runtime.started_at
            duration_s = (
                max(0.0, finished_at - started_at)
                if finished_at is not None and started_at is not None
                else None
            )
            finished_label = "Completed at" if terminal_reason == "completed" else "Finished at"

            status_details: list[str] = []
            if finished_at is not None:
                finished_text = datetime.fromtimestamp(finished_at).strftime("%Y-%m-%d %H:%M:%S")
                status_details.append(f"{finished_label}: {finished_text}")
            if duration_s is not None:
                status_details.append(f"Duration: {format_elapsed(duration_s)}")
            status_line = f"Status: {status}"
            if status_details:
                status_line = f"{status_line} ({', '.join(status_details)})"

            logger.debug(
                "publish_terminal_notifications: task={task_id} status={status} "
                "started_at={started_at} finished_at={finished_at} "
                "duration_s={duration_s} status_line={status_line}",
                task_id=view.spec.id,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                duration_s=duration_s,
                status_line=status_line,
            )

            body_lines = [
                f"Task ID: {view.spec.id}",
                status_line,
            ]
            body_lines.append(f"Description: {view.spec.description}")
            if terminal_reason != status:
                body_lines.append(f"Terminal reason: {terminal_reason}")
            if view.runtime.exit_code is not None:
                body_lines.append(f"Exit code: {view.runtime.exit_code}")
            if view.runtime.failure_reason:
                body_lines.append(f"Failure reason: {view.runtime.failure_reason}")

            event = NotificationEvent(
                id=self._notifications.new_id(),
                category="task",
                type=f"task.{terminal_reason}",
                source_kind="background_task",
                source_id=view.spec.id,
                title=title,
                body="\n".join(body_lines),
                severity=severity,
                payload={
                    "task_id": view.spec.id,
                    "task_kind": view.spec.kind,
                    "status": status,
                    "description": view.spec.description,
                    "exit_code": view.runtime.exit_code,
                    "interrupted": view.runtime.interrupted,
                    "timed_out": view.runtime.timed_out,
                    "terminal_reason": terminal_reason,
                    "failure_reason": view.runtime.failure_reason,
                    "finished_at": finished_at,
                    "duration_s": duration_s,
                },
                dedupe_key=f"background_task:{view.spec.id}:{terminal_reason}",
            )
            logger.debug(
                "publish_terminal_notifications: "
                "publishing event id={event_id} dedupe_key={dedupe_key} body={body}",
                event_id=event.id,
                dedupe_key=event.dedupe_key,
                body=event.body,
            )
            notification = self._notifications.publish(event)
            if notification.event.id == event.id:
                published.append(notification.event.id)
                self._completion_event.set()
                logger.debug(
                    "publish_terminal_notifications: event id={event_id} published successfully",
                    event_id=event.id,
                )
            else:
                logger.debug(
                    "publish_terminal_notifications: "
                    "event id={event_id} deduplicated to existing id={existing_id}",
                    event_id=event.id,
                    existing_id=notification.event.id,
                )
            if limit is not None and len(published) >= limit:
                break
        return published

    def _mark_task_running(self, task_id: str) -> None:
        runtime = self._store.read_runtime(task_id)
        if is_terminal_status(runtime.status):
            logger.debug(
                "_mark_task_running: task {task_id} already terminal ({status}), skipping",
                task_id=task_id,
                status=runtime.status,
            )
            return
        runtime.status = "running"
        runtime.updated_at = time.time()
        runtime.heartbeat_at = runtime.updated_at
        runtime.failure_reason = None
        self._store.write_runtime(task_id, runtime)
        logger.debug(
            "_mark_task_running: task {task_id} marked running at {ts}",
            task_id=task_id,
            ts=runtime.updated_at,
        )

    def _mark_task_awaiting_approval(self, task_id: str, reason: str) -> None:
        runtime = self._store.read_runtime(task_id)
        if is_terminal_status(runtime.status):
            return
        runtime.status = "awaiting_approval"
        runtime.updated_at = time.time()
        runtime.failure_reason = reason
        self._store.write_runtime(task_id, runtime)

    def _mark_task_completed(self, task_id: str) -> None:
        runtime = self._store.read_runtime(task_id)
        if is_terminal_status(runtime.status):
            logger.debug(
                "_mark_task_completed: task {task_id} already terminal ({status}), skipping",
                task_id=task_id,
                status=runtime.status,
            )
            return
        runtime.status = "completed"
        runtime.updated_at = time.time()
        runtime.finished_at = runtime.updated_at
        runtime.failure_reason = None
        self._store.write_runtime(task_id, runtime)
        logger.debug(
            "_mark_task_completed: task {task_id} marked completed "
            "at {finished_at} (started_at={started_at})",
            task_id=task_id,
            finished_at=runtime.finished_at,
            started_at=runtime.started_at,
        )
        from kimo_cli.telemetry import track

        if runtime.started_at and runtime.finished_at:
            duration = runtime.finished_at - runtime.started_at
            track("background_task_completed", success=True, duration_s=duration)

    def _mark_task_failed(self, task_id: str, reason: str) -> None:
        runtime = self._store.read_runtime(task_id)
        if is_terminal_status(runtime.status):
            return
        runtime.status = "failed"
        runtime.updated_at = time.time()
        runtime.finished_at = runtime.updated_at
        runtime.failure_reason = reason
        self._store.write_runtime(task_id, runtime)
        from kimo_cli.telemetry import track

        if runtime.started_at and runtime.finished_at:
            duration = runtime.finished_at - runtime.started_at
            track(
                "background_task_completed",
                success=False,
                duration_s=duration,
                reason="error",
            )

    def _mark_task_timed_out(self, task_id: str, reason: str) -> None:
        runtime = self._store.read_runtime(task_id)
        if is_terminal_status(runtime.status):
            return
        runtime.status = "failed"
        runtime.updated_at = time.time()
        runtime.finished_at = runtime.updated_at
        runtime.interrupted = True
        runtime.timed_out = True
        runtime.failure_reason = reason
        self._store.write_runtime(task_id, runtime)
        from kimo_cli.telemetry import track

        if runtime.started_at and runtime.finished_at:
            duration = runtime.finished_at - runtime.started_at
            track(
                "background_task_completed",
                success=False,
                duration_s=duration,
                reason="timeout",
            )

    def _mark_task_killed(self, task_id: str, reason: str) -> None:
        runtime = self._store.read_runtime(task_id)
        if is_terminal_status(runtime.status):
            return
        runtime.status = "killed"
        runtime.updated_at = time.time()
        runtime.finished_at = runtime.updated_at
        runtime.interrupted = True
        runtime.failure_reason = reason
        self._store.write_runtime(task_id, runtime)
        from kimo_cli.telemetry import track

        if runtime.started_at and runtime.finished_at:
            duration = runtime.finished_at - runtime.started_at
            track(
                "background_task_completed",
                success=False,
                duration_s=duration,
                reason="killed",
            )
