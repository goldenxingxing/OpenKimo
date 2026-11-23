from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from kimo_cli.memory import ANONYMOUS_USER_SENTINEL
from kimo_cli.web.runner.container import ContainerSessionProcess
from kimo_cli.web.runner.process import SessionProcess


class _FakeWorkerProcess:
    def __init__(self, *, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.stdin = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode


class _FakeLogger:
    def __init__(self) -> None:
        self.entries: list[str] = []

    def info(self, message: str, **kwargs: Any) -> None:
        self.entries.append(message.format(**kwargs))

    def debug(self, message: str, **kwargs: Any) -> None:
        self.entries.append(message.format(**kwargs))

    def warning(self, message: str, **kwargs: Any) -> None:
        self.entries.append(message.format(**kwargs))


async def _wait_forever(_self: ContainerSessionProcess) -> None:
    await asyncio.Event().wait()


def _configure_share(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    share = tmp_path / "gateway-share"
    host_share = tmp_path / "host-share"
    share.mkdir()
    host_share.mkdir()
    monkeypatch.setenv("KIMO_SHARE_DIR", str(share))
    monkeypatch.setenv("KIMO_SESSION_DATA_DIR", str(host_share))
    return share, host_share


def _write_session(
    share: Path,
    host_share: Path,
    session_id: UUID,
    *,
    state: str | None = None,
    owner_id: str | None = "alice",
) -> Path:
    relative = Path("sessions") / "work" / str(session_id)
    session_dir = share / relative
    session_dir.mkdir(parents=True)
    (host_share / relative).mkdir(parents=True)
    if state is None:
        state = json.dumps({"owner_id": owner_id})
    (session_dir / "state.json").write_text(state, encoding="utf-8")
    (host_share / relative / "state.json").write_text(state, encoding="utf-8")
    return session_dir


def _make_process(
    session_id: UUID,
    *,
    extra_env: dict[str, str] | None = None,
) -> ContainerSessionProcess:
    return ContainerSessionProcess(session_id, extra_env=extra_env)


@pytest.mark.asyncio
async def test_start_keeps_secrets_out_of_argv_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    share, host_share = _configure_share(monkeypatch, tmp_path)
    session_id = uuid4()
    _write_session(share, host_share, session_id)
    secret = "test-only-secret-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    fake_logger = _FakeLogger()
    fake_process = _FakeWorkerProcess()
    create_exec = AsyncMock(return_value=fake_process)

    monkeypatch.setattr("kimo_cli.web.runner.container.logger", fake_logger)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_exec)
    monkeypatch.setattr(ContainerSessionProcess, "_read_loop", _wait_forever)
    proc = _make_process(session_id, extra_env={"CUSTOM_TOKEN": secret})
    proc._cleanup_container = AsyncMock()

    await proc.start()
    await proc.stop_worker()

    await_args = create_exec.await_args
    assert await_args is not None
    cmd = list(await_args.args)
    env = await_args.kwargs["env"]
    assert env["OPENAI_API_KEY"] == secret
    assert env["CUSTOM_TOKEN"] == secret
    assert secret not in repr(cmd)
    assert secret not in repr(fake_logger.entries)
    env_args = [cmd[index + 1] for index, arg in enumerate(cmd) if arg == "-e"]
    assert "OPENAI_API_KEY" in env_args
    assert "CUSTOM_TOKEN" in env_args
    assert all("=" not in arg for arg in env_args)


def test_mounts_are_scoped_to_current_session_and_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    share, host_share = _configure_share(monkeypatch, tmp_path)
    session_id = uuid4()
    other_id = uuid4()
    session_dir = _write_session(share, host_share, session_id, owner_id="alice")
    _write_session(share, host_share, other_id, owner_id="bob")
    (share / "kimo.json").write_text("{}", encoding="utf-8")
    (host_share / "kimo.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CUSTOM_SKILLS_HOST_PATH", str(tmp_path / "skills"))
    monkeypatch.setenv("HF_CACHE_HOST_PATH", str(tmp_path / "hf"))

    cmd, env = _make_process(session_id)._build_docker_launch()
    volumes = [cmd[index + 1] for index, arg in enumerate(cmd) if arg == "-v"]

    assert f"{host_share}:{share}" not in volumes
    assert f"{host_share}:{share}:ro" not in volumes
    host_session_dir = host_share / "sessions" / "work" / str(session_id)
    assert f"{host_session_dir}:{session_dir}" in volumes
    memory = Path("users") / "alice" / "memory"
    assert f"{host_share / memory}:{share / memory}" in volumes
    assert f"{host_share / 'kimo.json'}:{share / 'kimo.json'}:ro" in volumes
    assert f"{tmp_path / 'skills'}:/root/.config/agents/skills:ro" in volumes
    assert f"{tmp_path / 'hf'}:/root/.cache/huggingface:ro" in volumes
    assert str(other_id) not in repr(volumes)
    assert "bob" not in repr(volumes)
    assert env["KIMO_USER_ID"] == "alice"


@pytest.mark.parametrize(
    "state",
    [
        "{not-json",
        json.dumps({"owner_id": "../other-user"}),
        json.dumps({"owner_id": 123}),
        json.dumps({"owner_id": ""}),
    ],
)
def test_invalid_existing_owner_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
) -> None:
    share, host_share = _configure_share(monkeypatch, tmp_path)
    session_id = uuid4()
    _write_session(share, host_share, session_id, state=state)

    with pytest.raises(ValueError):
        _make_process(session_id)._build_docker_launch()


def test_owner_disk_error_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    share, host_share = _configure_share(monkeypatch, tmp_path)
    session_id = uuid4()
    session_dir = _write_session(share, host_share, session_id)
    original_read_text = Path.read_text

    def fail_state_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == session_dir / "state.json":
            raise OSError("simulated disk failure")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_state_read)
    with pytest.raises(ValueError, match="owner safely"):
        _make_process(session_id)._build_docker_launch()


def test_missing_owner_state_for_existing_session_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    share, host_share = _configure_share(monkeypatch, tmp_path)
    session_id = uuid4()
    session_dir = _write_session(share, host_share, session_id)
    (session_dir / "state.json").unlink()

    with pytest.raises(ValueError, match="owner safely"):
        _make_process(session_id)._build_docker_launch()


def test_truly_new_session_retains_anonymous_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    share, host_share = _configure_share(monkeypatch, tmp_path)
    session_id = uuid4()

    cmd, env = _make_process(session_id)._build_docker_launch()

    assert env["KIMO_USER_ID"] == ANONYMOUS_USER_SENTINEL
    memory = Path("users") / ANONYMOUS_USER_SENTINEL / "memory"
    assert f"{host_share / memory}:{share / memory}" in [
        cmd[index + 1] for index, arg in enumerate(cmd) if arg == "-v"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("post-spawn"), asyncio.CancelledError()])
async def test_post_spawn_failure_cleans_up_and_allows_restart(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    session_id = uuid4()
    processes = [_FakeWorkerProcess(), _FakeWorkerProcess()]
    create_exec = AsyncMock(side_effect=processes)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_exec)
    monkeypatch.setattr(ContainerSessionProcess, "_read_loop", _wait_forever)
    proc = _make_process(session_id)
    proc._build_docker_launch = lambda: (["docker", "run"], {})
    proc._cleanup_container = AsyncMock()
    proc._emit_status = AsyncMock(side_effect=failure)

    with pytest.raises(type(failure)):
        await proc.start()

    assert proc._process is None
    assert proc._read_task is None
    assert proc._worker_id is None
    assert not proc.is_alive
    assert processes[0].terminated
    proc._cleanup_container.assert_awaited_once()

    proc._emit_status = AsyncMock(return_value=None)
    await proc.start()
    assert create_exec.await_count == 2
    assert proc.is_alive
    await proc.stop_worker()


@pytest.mark.asyncio
async def test_stop_removes_named_container_when_attached_cli_is_already_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _make_process(uuid4())
    proc._container_name = "kimo-session-dead-cli"
    proc._process = cast("asyncio.subprocess.Process", _FakeWorkerProcess(returncode=1))
    docker_process = AsyncMock()
    docker_process.wait = AsyncMock(return_value=0)
    create_exec = AsyncMock(return_value=docker_process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_exec)

    await proc.stop_worker()

    commands = [call.args for call in create_exec.await_args_list]
    assert commands == [
        ("docker", "stop", "--time", "5", "kimo-session-dead-cli"),
        ("docker", "rm", "--force", "kimo-session-dead-cli"),
    ]
    assert proc._process is None
    assert proc._worker_id is None


@pytest.mark.asyncio
async def test_read_loop_exit_removes_named_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _make_process(uuid4())
    proc._container_name = "kimo-session-cli-exited"
    proc._process = cast("asyncio.subprocess.Process", _FakeWorkerProcess(returncode=1))
    docker_process = AsyncMock()
    docker_process.wait = AsyncMock(return_value=0)
    create_exec = AsyncMock(return_value=docker_process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_exec)

    async def finish_read_loop(_self: SessionProcess) -> None:
        return

    monkeypatch.setattr(SessionProcess, "_read_loop", finish_read_loop)
    await proc._read_loop()

    commands = [call.args for call in create_exec.await_args_list]
    assert commands == [
        ("docker", "stop", "--time", "5", "kimo-session-cli-exited"),
        ("docker", "rm", "--force", "kimo-session-cli-exited"),
    ]
