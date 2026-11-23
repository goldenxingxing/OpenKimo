"""Containerized session process management using Docker.

This module provides ContainerSessionProcess and ContainerRunner, which
replace the local subprocess model with per-session Docker containers.

Communication model:
- Each session runs in its own Docker container (kimo-agent-sandbox image)
- The gateway executes ``docker run -i --rm`` as an asyncio subprocess
- stdin/stdout pipes are used for JSON-RPC communication (same protocol)
- Container lifecycle is explicitly managed by container name
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from kimo_cli import logger
from kimo_cli.memory import resolve_owner_id
from kimo_cli.utils.subprocess_env import get_clean_env
from kimo_cli.web.runner.activity import SessionActivity
from kimo_cli.web.runner.process import KimoCLIRunner, SessionProcess


@dataclass(frozen=True, slots=True)
class _SessionSecurityContext:
    session_dir: Path | None
    owner_id: str | None


def _read_session_security_context(session_id: UUID) -> _SessionSecurityContext:
    """Read owner and storage location without turning errors into anonymous access."""
    share = os.environ.get("KIMO_SHARE_DIR")
    if not share:
        return _SessionSecurityContext(session_dir=None, owner_id=None)

    sessions_root = Path(share) / "sessions"
    try:
        matches = list(sessions_root.glob(f"*/{session_id}"))
    except OSError as exc:
        raise RuntimeError(f"Cannot locate session storage safely: {session_id}") from exc
    if not matches:
        # A genuinely new session has no on-disk owner yet and retains the
        # legacy anonymous-memory behaviour.
        return _SessionSecurityContext(session_dir=None, owner_id=None)
    if len(matches) != 1 or not matches[0].is_dir():
        raise ValueError(f"Ambiguous or invalid session storage: {session_id}")

    session_dir = matches[0]
    try:
        raw: Any = json.loads((session_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read session owner safely: {session_id}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid session state format: {session_id}")

    owner_id = raw.get("owner_id")
    if owner_id is None:
        return _SessionSecurityContext(session_dir=session_dir, owner_id=None)
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError(f"Invalid session owner format: {session_id}")
    resolve_owner_id(owner_id)
    return _SessionSecurityContext(session_dir=session_dir, owner_id=owner_id)


def _read_owner_id_from_disk(session_id: UUID) -> str | None:
    """Read the persisted owner for this session, failing closed on disk errors."""
    return _read_session_security_context(session_id).owner_id


# Environment variable names to forward into sandbox containers. Values travel
# in the docker CLI's environment, never in its argv or startup logs.
_SANDBOX_ENV_VARS = (
    "KIMO_MODEL_MAX_CONTEXT_SIZE",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "LLM_PROVIDERS",
    "LLM_DEFAULT_PROVIDER",
    "LLM_PROVIDER",
    "LLM_THINKING",
    "LLM_TEMPERATURE",
    "KIMO_SHARE_DIR",
    "KIMO_SESSIONS_DIR",
    "KIMO_WORK_DIR",
    "KIMO_OUTPUT_DIR",
    "KIMO_DEBUG",
    "ENABLE_BROWSER",
    "ENABLE_JUPYTER",
    "ENABLE_SHELL_SANDBOX",
    "BLOCK_DANGEROUS_COMMANDS",
    "DISPLAY",
    "SCREEN_RESOLUTION",
    "CHROME_LOCALE",
    "TZ",
    "CHROME_INIT_URL",
    "CHROME_FLAGS",
    "USE_CDP",
    "HF_HOME",
    "HF_TOKEN",
    "HUGGINGFACE_HUB_CACHE",
    "TRANSFORMERS_CACHE",
    "NO_PROXY",
    "no_proxy",
)

# Worker inputs that are genuinely shared. Mount them individually so other
# users' sessions and memory are not visible inside this session's container.
_READ_ONLY_SHARED_PATHS = (
    "kimo.json",
    "config.toml",
    "config.json",
    "mcp.json",
    "mcp-oauth",
    "credentials",
    "device_id",
    "evolution",
    "plugins",
    "skills",
    "bin",
)

_DOCKER_STOP_TIMEOUT_SECONDS = 5
_DOCKER_COMMAND_TIMEOUT_SECONDS = 10


class ContainerSessionProcess(SessionProcess):
    """SessionProcess that runs the worker inside a Docker container."""

    def __init__(
        self,
        session_id: UUID,
        *,
        image: str = "kimo-agent-sandbox:latest",
        network: str | None = None,
        resource_limits: dict[str, str] | None = None,
        extra_env: dict[str, str] | None = None,
        on_activity: Callable[[SessionActivity], None] | None = None,
        on_retry: Callable[[SessionProcess], None] | None = None,
    ) -> None:
        super().__init__(session_id, on_activity=on_activity, on_retry=on_retry)
        self._image = image
        self._network = network
        self._resource_limits = resource_limits or {}
        self._extra_env = extra_env or {}
        self._container_name = f"kimo-session-{session_id}"

    @property
    def container_name(self) -> str:
        """Current container name (updated on each start)."""
        return self._container_name

    async def start(
        self,
        *,
        reason: str | None = None,
        detail: str | None = None,
        restart_started_at: float | None = None,
    ) -> None:
        """Start the KimoCLI worker inside a Docker container."""
        async with self._lock:
            if self.is_alive:
                if self._read_task is None or self._read_task.done():
                    self._read_task = asyncio.create_task(self._read_loop())
                return

            self._in_flight_prompt_ids.clear()
            self._expecting_exit = False
            self._worker_id = str(uuid4())
            # Avoid name conflicts while a previous --rm container is stopping.
            self._container_name = f"kimo-session-{self.session_id}-{self._worker_id[:8]}"

            try:
                cmd, env = self._build_docker_launch()
                logger.info(
                    "Starting container for session {session_id}: {cmd}",
                    session_id=self.session_id,
                    cmd=" ".join(cmd),
                )
                self._process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=16 * 1024 * 1024,
                    env=env,
                )
                self._worker_started_at = time.monotonic()
                self._read_task = asyncio.create_task(self._read_loop())
                await self._replay_initialize()
                if restart_started_at is not None:
                    elapsed_ms = int((time.perf_counter() - restart_started_at) * 1000)
                    detail = f"restart_ms={elapsed_ms}"
                    await self._emit_status("idle", reason=reason or "start", detail=detail)
                    await self._emit_restart_notice(reason=reason, restart_ms=elapsed_ms)
                else:
                    await self._emit_status("idle", reason=reason or "start", detail=detail)
            except BaseException:
                # Includes cancellation after spawn. A failed start must never
                # leave a live container or state that makes start() return early.
                await asyncio.shield(self._cleanup_failed_start())
                raise

    def _build_docker_cmd(self) -> list[str]:
        """Build the Docker command without embedding environment values in argv."""
        return self._build_docker_launch()[0]

    def _build_docker_launch(self) -> tuple[list[str], dict[str, str]]:
        cmd: list[str] = [
            "docker",
            "run",
            "-i",
            "--rm",
            "--name",
            self._container_name,
        ]
        cpus = self._resource_limits.get("cpus")
        if cpus:
            cmd.extend(["--cpus", cpus])
        memory = self._resource_limits.get("memory")
        if memory:
            cmd.extend(["--memory", memory])
        pids = self._resource_limits.get("pids")
        if pids:
            cmd.extend(["--pids-limit", pids])
        if self._network:
            cmd.extend(["--network", self._network])

        share_dir = Path(os.environ.get("KIMO_SHARE_DIR", "/data/sessions"))
        host_share_dir = Path(os.environ.get("KIMO_SESSION_DATA_DIR", str(share_dir)))
        context = _read_session_security_context(self.session_id)

        for name in _READ_ONLY_SHARED_PATHS:
            host_path = host_share_dir / name
            if host_path.exists():
                cmd.extend(["-v", f"{host_path}:{share_dir / name}:ro"])

        if context.session_dir is not None:
            relative_session_dir = context.session_dir.relative_to(share_dir)
            host_session_dir = host_share_dir / relative_session_dir
            cmd.extend(["-v", f"{host_session_dir}:{context.session_dir}"])

            # The worker needs a writable logs/ directory. Scope it to this
            # session instead of giving every container the shared logs tree.
            host_logs_dir = host_session_dir / "container-logs"
            host_logs_dir.mkdir(parents=True, exist_ok=True)
            cmd.extend(["-v", f"{host_logs_dir}:{share_dir / 'logs'}"])

        custom_skills = os.environ.get("CUSTOM_SKILLS_HOST_PATH")
        if custom_skills:
            cmd.extend(["-v", f"{custom_skills}:/root/.config/agents/skills:ro"])

        hf_cache = os.environ.get("HF_CACHE_HOST_PATH")
        if hf_cache:
            cmd.extend(["-v", f"{hf_cache}:/root/.cache/huggingface:ro"])

        owner_id = resolve_owner_id(context.owner_id)
        host_memory_dir = host_share_dir / "users" / owner_id / "memory"
        host_memory_dir.mkdir(parents=True, exist_ok=True)
        container_memory_dir = share_dir / "users" / owner_id / "memory"
        cmd.extend(["-v", f"{host_memory_dir}:{container_memory_dir}"])

        forwarded_env = {name: os.environ[name] for name in _SANDBOX_ENV_VARS if name in os.environ}
        forwarded_env.update(self._extra_env)
        forwarded_env["KIMO_SESSION_ID"] = str(self.session_id)
        forwarded_env["KIMO_USER_ID"] = owner_id
        if hf_cache:
            forwarded_env["HF_HOME"] = "/root/.cache/huggingface"
        for name in forwarded_env:
            cmd.extend(["-e", name])

        cmd.extend(["--privileged=false", self._image, "/start-sandbox.sh"])
        env = get_clean_env()
        env.update(forwarded_env)
        return cmd, env

    async def _run_docker_cleanup_command(self, *args: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=_DOCKER_COMMAND_TIMEOUT_SECONDS)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError):
            logger.warning(
                "Docker cleanup command unavailable or timed out for {name}",
                name=self._container_name,
            )

    async def _cleanup_container(self) -> None:
        """Bounded stop/rm by name, independent of the attached CLI's state."""
        await self._run_docker_cleanup_command(
            "stop", "--time", str(_DOCKER_STOP_TIMEOUT_SECONDS), self._container_name
        )
        await self._run_docker_cleanup_command("rm", "--force", self._container_name)

    async def _cancel_read_task(self) -> None:
        task = self._read_task
        self._read_task = None
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _reset_worker_state(self) -> None:
        self._process = None
        self._worker_started_at = None
        self._worker_id = None
        self._in_flight_prompt_ids.clear()
        self._expecting_exit = False

    async def _cleanup_failed_start(self) -> None:
        self._expecting_exit = True
        process = self._process
        try:
            if process is not None:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError, OSError):
                        process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10.0)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError, OSError):
                        process.kill()
                    await asyncio.wait_for(process.wait(), timeout=1.0)
        except (OSError, TimeoutError):
            pass
        finally:
            self._process = None
            await self._cancel_read_task()
            await self._cleanup_container()
            self._reset_worker_state()

    async def stop_worker(
        self,
        *,
        reason: str | None = None,
        emit_status: bool = True,
    ) -> None:
        """Stop the attached CLI and remove the named container, even if CLI died."""
        try:
            await super().stop_worker(reason=reason, emit_status=emit_status)
        finally:
            self._process = None
            await self._cancel_read_task()
            self._reset_worker_state()
            await asyncio.shield(self._cleanup_container())

    async def _read_loop(self) -> None:
        try:
            await super()._read_loop()
        finally:
            if not self._expecting_exit:
                # The attached docker CLI can exit while the container survives.
                await asyncio.shield(self._cleanup_container())


class ContainerRunner(KimoCLIRunner):
    """Manages multiple session processes inside Docker containers."""

    def __init__(
        self,
        *,
        image: str = "kimo-agent-sandbox:latest",
        network: str | None = None,
        resource_limits: dict[str, str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._image = image
        self._network = network
        self._resource_limits = resource_limits or {}
        self._extra_env = extra_env or {}

    def start(self) -> None:
        """Start the runner (no-op, containers started on demand)."""

    def get_session(self, session_id: UUID) -> ContainerSessionProcess | None:
        proc = self._sessions.get(session_id)
        if proc is None:
            return None
        assert isinstance(proc, ContainerSessionProcess)
        return proc

    async def get_or_create_session(self, session_id: UUID) -> ContainerSessionProcess:
        async with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = ContainerSessionProcess(
                    session_id,
                    image=self._image,
                    network=self._network,
                    resource_limits=self._resource_limits,
                    extra_env=self._extra_env,
                    on_activity=self.activity.publish,
                    on_retry=self._stop_retry_if_unattended,
                )
            proc = self._sessions[session_id]
            assert isinstance(proc, ContainerSessionProcess)
            return proc


ContainerRunner.__module__ = "kimo_cli.web.runner.process"
