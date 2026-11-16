from __future__ import annotations

import asyncio
import time

import pytest

from kimo_cli.background import (
    BackgroundTaskStore,
    TaskControl,
    TaskSpec,
    run_background_task_worker,
)
from kimo_cli.background import store as store_module
from kimo_cli.background.worker import terminate_process_tree_windows


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
        shell_path="/bin/bash",
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
        shell_path="/bin/bash",
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
        shell_path="/bin/bash",
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
        shell_path="/bin/bash",
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
        shell_path="/bin/bash",
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
