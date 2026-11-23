from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kimo_cli.background import (
    BackgroundTaskStore,
    TaskControl,
    TaskSpec,
    run_background_task_worker,
)
from kimo_cli.background import store as store_module
from kimo_cli.background.worker import terminate_process_tree_windows


def _process_is_alive(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    )
    state = result.stdout.strip()
    return bool(state) and not state.startswith("Z")


async def _wait_for_path(path: Path) -> None:
    async with asyncio.timeout(3):
        while not path.exists() or not path.read_text(encoding="utf-8").strip():
            await asyncio.sleep(0.01)


async def _assert_process_stopped(pid: int) -> None:
    async with asyncio.timeout(3):
        while _process_is_alive(pid):
            await asyncio.sleep(0.02)


def _worker_spec_with_sigterm_ignoring_child(runtime, task_id: str, pid_file: Path) -> TaskSpec:
    child_code = (
        "import os, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1], 'w').write(str(os.getpid())); "
        "time.sleep(30)"
    )
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(child_code)} "
        f"{shlex.quote(str(pid_file))} & sleep 30"
    )
    return TaskSpec(
        id=task_id,
        kind="bash",
        session_id=runtime.session.id,
        description="worker process tree cleanup",
        tool_call_id=f"tool-{task_id}",
        command=command,
        shell_name="bash",
        shell_path="/bin/bash",
        cwd=str(runtime.session.work_dir),
        timeout_s=30,
    )


@pytest.mark.asyncio
async def test_worker_completes_successfully(runtime):
    store = BackgroundTaskStore(runtime.session.context_file.parent / "tasks")
    spec = TaskSpec(
        id="b3333333",
        kind="bash",
        session_id=runtime.session.id,
        description="echo hello",
        tool_call_id="tool-4",
        command="echo hello",
        shell_name="bash",
        shell_path=str(runtime.environment.shell_path),
        cwd=str(runtime.session.work_dir),
        timeout_s=60,
    )
    store.create_task(spec)

    await run_background_task_worker(store.task_dir(spec.id), heartbeat_interval_ms=50)

    view = store.merged_view(spec.id)
    assert view.runtime.status == "completed"
    assert view.runtime.exit_code == 0
    assert "hello" in store.output_path(spec.id).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_worker_respects_kill_control(runtime):
    store = BackgroundTaskStore(runtime.session.context_file.parent / "tasks")
    spec = TaskSpec(
        id="b4444444",
        kind="bash",
        session_id=runtime.session.id,
        description="sleep task",
        tool_call_id="tool-5",
        command="sleep 5",
        shell_name="bash",
        shell_path=str(runtime.environment.shell_path),
        cwd=str(runtime.session.work_dir),
        timeout_s=60,
    )
    store.create_task(spec)

    worker_task = asyncio.create_task(
        run_background_task_worker(
            store.task_dir(spec.id),
            heartbeat_interval_ms=50,
            control_poll_interval_ms=20,
            kill_grace_period_ms=50,
        )
    )
    await asyncio.sleep(0.2)
    store.write_control(
        spec.id,
        TaskControl(
            kill_requested_at=time.time(),
            kill_reason="stop test",
            force=False,
        ),
    )
    await worker_task

    view = store.merged_view(spec.id)
    assert view.runtime.status == "killed"
    assert view.runtime.interrupted is True
    assert view.runtime.failure_reason == "stop test"


@pytest.mark.asyncio
async def test_worker_marks_timeout_as_failed(runtime):
    store = BackgroundTaskStore(runtime.session.context_file.parent / "tasks")
    spec = TaskSpec(
        id="b5554444",
        kind="bash",
        session_id=runtime.session.id,
        description="timeout task",
        tool_call_id="tool-6",
        command="sleep 2",
        shell_name="bash",
        shell_path=str(runtime.environment.shell_path),
        cwd=str(runtime.session.work_dir),
        timeout_s=1,
    )
    store.create_task(spec)

    await run_background_task_worker(
        store.task_dir(spec.id),
        heartbeat_interval_ms=50,
        control_poll_interval_ms=20,
        kill_grace_period_ms=50,
    )

    view = store.merged_view(spec.id)
    assert view.runtime.status == "failed"
    assert view.runtime.interrupted is True
    assert view.runtime.timed_out is True
    assert view.runtime.failure_reason == "Command timed out after 1s"


@pytest.mark.asyncio
async def test_worker_enables_pipefail_for_bash(runtime):
    """A pipeline whose last stage succeeds must still fail when an earlier
    stage fails — without pipefail the recorded exit code is tail's 0."""
    store = BackgroundTaskStore(runtime.session.context_file.parent / "tasks")
    spec = TaskSpec(
        id="bpipefail",
        kind="bash",
        session_id=runtime.session.id,
        description="failing pipeline",
        tool_call_id="tool-pipe",
        command="false | true",
        shell_name="bash",
        shell_path=str(runtime.environment.shell_path),
        cwd=str(runtime.session.work_dir),
        timeout_s=60,
    )
    store.create_task(spec)

    await run_background_task_worker(store.task_dir(spec.id), heartbeat_interval_ms=50)

    view = store.merged_view(spec.id)
    assert view.runtime.status == "failed"
    assert view.runtime.exit_code != 0


@pytest.mark.asyncio
async def test_worker_heartbeat_skips_fsync(runtime, monkeypatch):
    """Heartbeat writes must not fsync: a stalled disk would otherwise freeze
    the worker's event loop and starve the heartbeat itself. The final
    terminal-state write keeps the durable fsync."""
    calls: list[tuple[bool, str]] = []
    real_atomic_json_write = store_module.atomic_json_write

    def recording_atomic_json_write(data, path, *, fsync=True):
        calls.append((fsync, path.name))
        return real_atomic_json_write(data, path, fsync=fsync)

    monkeypatch.setattr(store_module, "atomic_json_write", recording_atomic_json_write)

    task_store = BackgroundTaskStore(runtime.session.context_file.parent / "tasks")
    spec = TaskSpec(
        id="bnofsyncc",
        kind="bash",
        session_id=runtime.session.id,
        description="short task",
        tool_call_id="tool-fsync",
        command="sleep 0.3",
        shell_name="bash",
        shell_path=str(runtime.environment.shell_path),
        cwd=str(runtime.session.work_dir),
        timeout_s=60,
    )
    task_store.create_task(spec)

    await run_background_task_worker(task_store.task_dir(spec.id), heartbeat_interval_ms=20)

    runtime_writes = [fsync for fsync, name in calls if name == "runtime.json"]
    assert runtime_writes, "expected at least one runtime.json write"
    assert any(fsync is False for fsync in runtime_writes)
    # The terminal state write is the last one and stays durable.
    assert runtime_writes[-1] is True


def test_terminate_process_tree_windows_uses_taskkill_tree(monkeypatch):
    calls: list[list[str]] = []

    def _run(args, **kwargs):
        calls.append(args)
        return None

    monkeypatch.setattr("kimo_cli.background.worker.subprocess.run", _run)

    terminate_process_tree_windows(1234, force=False)
    terminate_process_tree_windows(1234, force=True)

    assert calls == [
        ["taskkill", "/PID", "1234", "/T"],
        ["taskkill", "/PID", "1234", "/T", "/F"],
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.asyncio
async def test_worker_kill_escalates_to_descendant_after_leader_exits(runtime, tmp_path: Path):
    manager = runtime.background_tasks
    store = manager.store
    pid_file = tmp_path / "worker-child.pid"
    spec = _worker_spec_with_sigterm_ignoring_child(runtime, "b-treekill", pid_file)
    store.create_task(spec)
    worker = asyncio.create_task(
        run_background_task_worker(
            store.task_dir(spec.id),
            heartbeat_interval_ms=20,
            control_poll_interval_ms=10,
            kill_grace_period_ms=50,
        )
    )
    child_pid: int | None = None
    try:
        await _wait_for_path(pid_file)
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        manager.kill(spec.id, reason="test process tree kill")
        await asyncio.wait_for(worker, timeout=5)

        assert store.read_runtime(spec.id).status == "killed"
        assert store.read_runtime(spec.id).failure_reason == "test process tree kill"
        await _assert_process_stopped(child_pid)
    finally:
        if child_pid is not None and _process_is_alive(child_pid):
            os.kill(child_pid, 9)
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.asyncio
async def test_worker_cancellation_cleans_child_and_re_raises(runtime, tmp_path: Path):
    store = runtime.background_tasks.store
    pid_file = tmp_path / "cancel-child.pid"
    spec = _worker_spec_with_sigterm_ignoring_child(runtime, "b-treecancel", pid_file)
    store.create_task(spec)
    worker = asyncio.create_task(
        run_background_task_worker(
            store.task_dir(spec.id),
            heartbeat_interval_ms=20,
            control_poll_interval_ms=10,
            kill_grace_period_ms=20,
        )
    )
    child_pid: int | None = None
    try:
        await _wait_for_path(pid_file)
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

        final = store.read_runtime(spec.id)
        assert final.status == "killed"
        assert final.interrupted is True
        assert final.failure_reason == "Worker cancelled"
        await _assert_process_stopped(child_pid)
    finally:
        if child_pid is not None and _process_is_alive(child_pid):
            os.kill(child_pid, 9)
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.asyncio
async def test_heartbeat_failure_immediately_cleans_process_tree(
    runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = runtime.background_tasks.store
    pid_file = tmp_path / "heartbeat-failure-child.pid"
    spec = _worker_spec_with_sigterm_ignoring_child(runtime, "b-helperfail", pid_file)
    store.create_task(spec)
    original_write_runtime = BackgroundTaskStore.write_runtime
    failed = False

    def fail_first_heartbeat(self, task_id, task_runtime, *, fsync=True):
        nonlocal failed
        # Only fail once the child has actually started and written its pid
        # file; otherwise the cleanup can kill the tree before the test can
        # observe the child (a race that is easy to hit on slow runners).
        if not fsync and task_runtime.status == "running" and not failed and pid_file.exists():
            failed = True
            raise OSError("heartbeat disk failed")
        original_write_runtime(self, task_id, task_runtime, fsync=fsync)

    monkeypatch.setattr(BackgroundTaskStore, "write_runtime", fail_first_heartbeat)
    child_pid: int | None = None
    try:
        await asyncio.wait_for(
            run_background_task_worker(
                store.task_dir(spec.id),
                heartbeat_interval_ms=20,
                control_poll_interval_ms=10,
                kill_grace_period_ms=20,
            ),
            timeout=5,
        )

        final = store.read_runtime(spec.id)
        assert failed is True
        assert final.status == "failed"
        assert final.failure_reason == "heartbeat disk failed"
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        await _assert_process_stopped(child_pid)
    finally:
        if child_pid is not None and _process_is_alive(child_pid):
            os.kill(child_pid, 9)
