import asyncio
import codecs
import contextlib
import os
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self, override

import kaos
from kaos import AsyncReadable
from kaos.local import LocalKaos
from kaos.path import KaosPath
from kosong.tooling import CallableTool2, ToolReturnValue
from pydantic import BaseModel, Field, model_validator

from kimo_cli.background import TaskView, format_task
from kimo_cli.soul.agent import Runtime
from kimo_cli.soul.approval import Approval
from kimo_cli.soul.toolset import get_current_tool_call_or_none
from kimo_cli.tools.display import BackgroundTaskDisplayBlock, ShellDisplayBlock
from kimo_cli.tools.utils import ToolResultBuilder, load_desc
from kimo_cli.utils.environment import Environment
from kimo_cli.utils.logging import logger
from kimo_cli.utils.shell_quoting import rewrite_windows_null_redirect
from kimo_cli.utils.subprocess_env import get_noninteractive_env

MAX_FOREGROUND_TIMEOUT = 5 * 60
MAX_BACKGROUND_TIMEOUT = 24 * 60 * 60
_STREAM_CHUNK_SIZE = 64 * 1024
_CLEANUP_GRACE_S = 0.2
_PROCESS_WAIT_TIMEOUT_S = 5.0


class Params(BaseModel):
    command: str = Field(description="The command to execute.")
    timeout: int = Field(
        description=(
            "The timeout in seconds for the command to execute. "
            "If the command takes longer than this, it will be killed. "
            "Foreground calls are rejected above 300s — for anything longer "
            "use run_in_background=true (up to 86400s). The default (60s) "
            "kills slow builds and tests unexpectedly often; set an explicit "
            "timeout that fits the command, or run it in the background."
        ),
        default=60,
        ge=1,
        le=MAX_BACKGROUND_TIMEOUT,
    )
    run_in_background: bool = Field(
        default=False,
        description="Whether to run the command as a background task.",
    )
    description: str = Field(
        default="",
        description=(
            "A short description for the background task. Required when run_in_background=true."
        ),
    )

    @model_validator(mode="after")
    def _validate_background_fields(self) -> Self:
        if self.run_in_background and not self.description.strip():
            raise ValueError("description is required when run_in_background is true")
        if not self.run_in_background and self.timeout > MAX_FOREGROUND_TIMEOUT:
            raise ValueError(
                f"timeout must be <= {MAX_FOREGROUND_TIMEOUT}s for foreground commands; "
                f"use run_in_background=true for longer timeouts (up to {MAX_BACKGROUND_TIMEOUT}s)"
            )
        return self


class Shell(CallableTool2[Params]):
    name: str = "Shell"
    params: type[Params] = Params

    def __init__(self, approval: Approval, environment: Environment, runtime: Runtime):
        super().__init__(
            description=load_desc(
                Path(__file__).parent / "bash.md",
                {"SHELL": environment.shell_description},
            )
        )
        self._approval = approval
        self._shell_name = environment.shell_name or "sh"
        self._shell_path = environment.shell_path
        self._shell_error = environment.shell_error
        self._on_windows = environment.os_kind == "Windows"
        self._runtime = runtime

    def _unavailable(self) -> ToolReturnValue:
        """Refuse the call when the host has no shell to run it with.

        Reported per call rather than at startup: the rest of the session is
        perfectly usable without a shell, and the message has to reach the user
        at the moment it actually matters.
        """
        return ToolResultBuilder().error(
            self._shell_error or "No shell is available on this machine.",
            brief="No shell available",
        )

    def preprocess_command(self, command: str) -> str:
        """Apply platform-specific defensive rewrites before execution.

        Public so the ACP terminal path can apply the same rewrites as local
        execution; skipping them there made the same command behave differently
        depending on the client.
        """
        return rewrite_windows_null_redirect(command, on_windows=self._on_windows)

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        builder = ToolResultBuilder()

        if not params.command:
            return builder.error("Command cannot be empty.", brief="Empty command")

        if self._shell_path is None:
            return self._unavailable()

        if params.run_in_background:
            return await self._run_in_background(params)

        command = self.preprocess_command(params.command)

        result = await self._approval.request(
            self.name,
            "run command",
            f"Run command `{command}`",
            display=[
                ShellDisplayBlock(
                    language="bash",
                    command=command,
                )
            ],
        )
        if not result:
            return result.rejection_error()

        def stdout_cb(line: bytes):
            line_str = line.decode(encoding="utf-8", errors="replace")
            builder.write(line_str)

        def stderr_cb(line: bytes):
            line_str = line.decode(encoding="utf-8", errors="replace")
            builder.write(line_str)

        try:
            exitcode = await self._run_shell_command(command, stdout_cb, stderr_cb, params.timeout)

            if exitcode == 0:
                return builder.ok("Command executed successfully.")
            else:
                brief = f"Failed with exit code: {exitcode}"
                tail = builder.tail()
                if tail:
                    brief += f"\n{tail}"
                return builder.error(
                    f"Command failed with exit code: {exitcode}.",
                    brief=brief,
                )
        except TimeoutError:
            return builder.error(
                f"Command killed by timeout ({params.timeout}s)",
                brief=f"Killed by timeout ({params.timeout}s)",
            )
        except Exception as e:
            logger.error(
                "Shell command execution failed: {command}: {error}",
                command=params.command,
                error=e,
            )
            return builder.error(
                f"Command execution failed: {e}",
                brief="Execution failed",
            )

    async def _run_in_background(self, params: Params) -> ToolReturnValue:
        tool_call = get_current_tool_call_or_none()
        if tool_call is None:
            return ToolResultBuilder().error(
                "Background shell requires a tool call context.",
                brief="No tool call context",
            )

        command = self.preprocess_command(params.command)

        result = await self._approval.request(
            self.name,
            "run background command",
            f"Run background command `{command}`",
            display=[
                ShellDisplayBlock(
                    language="bash",
                    command=command,
                )
            ],
        )
        if not result:
            return result.rejection_error()

        try:
            view = self._runtime.background_tasks.create_bash_task(
                command=command,
                description=params.description.strip(),
                timeout_s=params.timeout,
                tool_call_id=tool_call.id,
                shell_name=self._shell_name,
                shell_path=str(self._shell_path),
                cwd=str(self._runtime.session.work_dir),
            )
        except Exception as exc:
            logger.error(
                "Failed to start background shell task: {command}: {error}",
                command=params.command,
                error=exc,
            )
            builder = ToolResultBuilder()
            return builder.error(f"Failed to start background task: {exc}", brief="Start failed")

        return self._background_ok(view)

    def _background_ok(self, view: TaskView) -> ToolReturnValue:
        builder = ToolResultBuilder()
        builder.write(
            "\n".join(
                [
                    format_task(view, include_command=True),
                    "automatic_notification: true",
                    "next_step: You will be automatically notified when it completes.",
                    (
                        "next_step: Use TaskOutput with this task_id for a non-blocking "
                        "status/output snapshot. Only set block=true when you intentionally "
                        "want to wait."
                    ),
                    "next_step: Use TaskStop only if the task must be cancelled.",
                    (
                        "human_shell_hint: For users in the interactive shell, "
                        "the only task-management slash command is /task. "
                        "Do not suggest /task list, /task output, /task stop, or /tasks."
                    ),
                ]
            )
        )
        builder.display(
            BackgroundTaskDisplayBlock(
                task_id=view.spec.id,
                kind=view.spec.kind,
                status=view.runtime.status,
                description=view.spec.description,
            )
        )
        return builder.ok("Background task started", brief=f"Started {view.spec.id}")

    async def _spawn_process(self, command: str, env: dict[str, str]) -> tuple[Any, bool]:
        """Return the process and whether its PID is also a private PGID."""
        current_kaos = kaos.get_current_kaos()
        local_host = isinstance(current_kaos, LocalKaos) and self._on_windows == (os.name == "nt")
        if not local_host:
            return await kaos.exec(*self._shell_args(command), env=env), False

        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": env,
            "cwd": str(KaosPath.cwd()),
            # Chunked reads below do not depend on readline's separator limit.
            "limit": _STREAM_CHUNK_SIZE,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(*self._shell_args(command), **kwargs)
        return process, os.name != "nt"

    async def _run_shell_command(
        self,
        command: str,
        stdout_cb: Callable[[bytes], None],
        stderr_cb: Callable[[bytes], None],
        timeout: int,
    ) -> int:
        async def _read_stream(stream: AsyncReadable, cb: Callable[[bytes], None]) -> None:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while chunk := await stream.read(_STREAM_CHUNK_SIZE):
                text = decoder.decode(chunk)
                if text:
                    cb(text.encode("utf-8"))
            tail = decoder.decode(b"", final=True)
            if tail:
                cb(tail.encode("utf-8"))

        async def _signal_process(force: bool) -> None:
            if process is None:
                return
            if private_pgid:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
                return
            if os.name == "nt":
                args = ["taskkill", "/PID", str(process.pid), "/T"]
                if force:
                    args.append("/F")
                await asyncio.to_thread(
                    subprocess.run,
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=_PROCESS_WAIT_TIMEOUT_S,
                )
                return
            with contextlib.suppress(ProcessLookupError):
                await process.kill()

        async def _cleanup_process() -> None:
            if process is None:
                return
            await _signal_process(force=False)
            if private_pgid or os.name == "nt":
                if private_pgid:
                    await asyncio.sleep(_CLEANUP_GRACE_S)
                await _signal_process(force=True)
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=_PROCESS_WAIT_TIMEOUT_S)
                except TimeoutError:
                    logger.error(
                        "Timed out waiting for shell process {pid} after cleanup",
                        pid=process.pid,
                    )

        env = get_noninteractive_env()
        # Override SHELL so commands that read $SHELL see the bash we're actually
        # running, not an empty/stale value inherited from the parent (most visible
        # on Windows, where the parent's SHELL is typically empty or PowerShell).
        env["SHELL"] = str(self._shell_path)
        process: Any | None = None
        private_pgid = False
        readers: tuple[asyncio.Task[None], ...] = ()
        try:
            spawned_process, private_pgid = await self._spawn_process(command, env)
            process = spawned_process
            # Close stdin immediately so interactive prompts (e.g. git password)
            # get EOF instead of hanging forever waiting for input.
            spawned_process.stdin.close()
            readers = (
                asyncio.create_task(_read_stream(spawned_process.stdout, stdout_cb)),
                asyncio.create_task(_read_stream(spawned_process.stderr, stderr_cb)),
            )
            results = await asyncio.wait_for(
                asyncio.gather(*readers, spawned_process.wait()),
                timeout,
            )
            return results[2]
        except BaseException:
            # Timeout, cancellation, reader failures, and post-spawn errors all
            # share the same process-tree cleanup and bounded wait.
            await _cleanup_process()
            raise
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)

    def _shell_args(self, command: str) -> tuple[str, ...]:
        # Enable pipefail for bash so a pipeline's exit code reflects the
        # failing stage (``python x.py | tail`` must not report tail's 0).
        if self._shell_name == "bash":
            command = f"set -o pipefail; {command}"
        return (str(self._shell_path), "-c", command)
