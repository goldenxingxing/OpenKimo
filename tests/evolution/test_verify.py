"""Tests for kimo_cli.evolution.verify (no real subprocesses or LLM calls)."""

import asyncio
import json
import os
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from kimo_cli.config import get_default_config
from kimo_cli.evolution import library, verify


@pytest.fixture
def share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))
    return tmp_path


def _config(enabled: bool = True):
    config = get_default_config()
    config.default_self_evolve = enabled
    return config


def _records_line(task: str, outcome: str = "completed", tokens: int = 100, steps: int = 5) -> str:
    return json.dumps(
        {
            "outcome": outcome,
            "steps": steps,
            "input_tokens": tokens,
            "output_tokens": tokens // 2,
            "reasoning_tokens": 0,
            "duration_ms": 1000,
            "task": task,
        }
    )


def _write_records(share: Path, session: str, lines: list[str]) -> None:
    d = share / "sessions" / session / "evolution"
    d.mkdir(parents=True)
    (d / "records.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_proposal(share: Path, pid: str) -> Path:
    p = share / "evolution" / "proposals" / pid
    p.mkdir(parents=True)
    library.save_meta(p, {"id": pid, "status": "proposed"})
    (p / "system_append.md").write_text("candidate guidance", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# compare()
# ---------------------------------------------------------------------------


def _record(outcome="completed", steps=5, total=150):
    return verify.RunRecord(outcome=outcome, steps=steps, input_tokens=total, output_tokens=0)


def test_compare_candidate_cheaper():
    ev = verify.compare("t", _record(total=200, steps=6), _record(total=100, steps=3))
    assert ev.verdict_suggestion == "candidate_looks_better"
    assert ev.token_delta_pct == -50.0
    assert ev.steps_delta == -3


def test_compare_outcome_dominates():
    assert verify.compare(
        "t", _record(outcome="failed"), _record(outcome="completed")
    ).verdict_suggestion == ("candidate_looks_better")
    # Reverse
    assert verify.compare(
        "t", _record(outcome="completed"), _record(outcome="failed")
    ).verdict_suggestion == ("baseline_looks_better")


def test_compare_both_failed_inconclusive():
    ev = verify.compare("t", _record(outcome="failed"), _record(outcome="failed"))
    assert ev.verdict_suggestion == "inconclusive"


def test_compare_missing_record_inconclusive():
    missing = verify.RunRecord(note="record_missing")
    assert verify.compare("t", missing, _record()).verdict_suggestion == "inconclusive"


def test_compare_within_noise_inconclusive():
    ev = verify.compare("t", _record(total=100, steps=5), _record(total=103, steps=5))
    assert ev.verdict_suggestion == "inconclusive"


# ---------------------------------------------------------------------------
# select_tasks()
# ---------------------------------------------------------------------------


def test_select_tasks_prefers_completed(share: Path):
    _write_records(
        share,
        "s1",
        [
            _records_line("failed only task", outcome="failed"),
            _records_line("good task A"),
            _records_line("good task A"),
            _records_line("good task B"),
        ],
    )
    tasks = verify.select_tasks(_config(), limit=5)
    assert "good task A" in tasks
    assert "failed only task" not in tasks  # never completed: not replayable


def test_select_tasks_empty(share: Path):
    assert verify.select_tasks(_config()) == []


def test_select_tasks_rejects_side_effect_tasks(share: Path):
    """R3: tasks with background-launch / long-running intent must never be
    replayed by the unattended verify subprocess."""
    _write_records(
        share,
        "s1",
        [
            _records_line("用 nohup 后台跑寻优脚本，结果写到 output"),
            _records_line("帮我启动 DE 批量寻优，start_new_session 脱离会话"),
            _records_line("正常任务：对比一下这两个文档的差异"),
        ],
    )
    tasks = verify.select_tasks(_config(), limit=5)
    assert tasks == ["正常任务：对比一下这两个文档的差异"]


def test_select_tasks_rejects_system_reminder_turns(share: Path):
    """2026-11-19 incident: a background-notification turn was replayed; the
    verify agent relaunched the user's dead compute job from task_progress.md.
    Notification turns are not self-contained tasks and must be filtered."""
    _write_records(
        share,
        "s1",
        [
            _records_line(
                "<system-reminder>Background tasks completed while you were idle.</system-reminder>"
            ),
            _records_line("总结今天的改动"),
        ],
    )
    tasks = verify.select_tasks(_config(), limit=5)
    assert tasks == ["总结今天的改动"]


def test_select_tasks_rejects_context_dependent_prompts(share: Path):
    _write_records(share, "s1", [_records_line("继续"), _records_line("okay")])
    assert verify.select_tasks(_config(), limit=5) == []


def test_select_tasks_all_filtered_returns_empty(share: Path):
    """No fallback to an unsafe task: empty means the caller skips verifying."""
    _write_records(
        share,
        "s1",
        [
            _records_line("继续"),
            _records_line("nohup 部署到生产环境"),
        ],
    )
    assert verify.select_tasks(_config(), limit=5) == []


def test_is_verify_safe_task_patterns():
    safe = verify.is_verify_safe_task
    # Side-effect intent, EN + ZH spellings.
    assert not safe("run the benchmark with nohup in the background")
    assert not safe("setsid python train.py")
    assert not safe("用 subprocess start_new_session 启动")
    assert not safe("后台运行这个仿真")
    assert not safe("启动批量寻优，结果写工作区")
    assert not safe("部署发版并 git push")
    # Notification / context-dependent turns.
    assert not safe("<system-reminder>Background tasks completed</system-reminder>")
    assert not safe("继续")
    assert not safe("Okay")
    # Ordinary tasks stay selectable.
    assert safe("对比一下这两个文档的差异")
    assert safe("fix the failing test in web/src/hooks")
    assert safe("把报告写到 output 目录")
    assert not safe("")
    assert not safe("   ")


# ---------------------------------------------------------------------------
# verify() end-to-end with an injected fake runner
# ---------------------------------------------------------------------------


async def test_verify_dual_run_writes_evidence(share: Path):
    _write_records(share, "s1", [_records_line("fix the bug"), _records_line("fix the bug")])
    _make_proposal(share, "prop1")

    calls: list[dict] = []

    async def fake_runner(task, env_extra, cwd, out_file):
        calls.append({"task": task, "env": dict(env_extra)})
        if env_extra:
            return verify.RunRecord(
                outcome="completed", steps=3, input_tokens=100, output_tokens=50
            )
        return verify.RunRecord(outcome="completed", steps=6, input_tokens=200, output_tokens=100)

    evidence_path = await verify.verify(_config(), "prop1", limit=1, runner=fake_runner)

    assert evidence_path.exists()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["overall_verdict_suggestion"] == "candidate_looks_better"
    assert len(evidence["tasks"]) == 1
    assert evidence["tasks"][0]["token_delta_pct"] == -50.0
    assert "clean context" in evidence["caveat"]
    # One baseline run (no env) + one candidate run (patch env injected).
    assert len(calls) == 2
    assert calls[0]["env"] == {}
    assert library.PATCH_ENV_VAR in calls[1]["env"]


async def test_verify_refused_when_switch_off(share: Path):
    _write_records(share, "s1", [_records_line("a task")])
    _make_proposal(share, "prop1")
    with pytest.raises(library.EvolutionGateError):
        await verify.verify(_config(enabled=False), "prop1", runner=None)


async def test_verify_unknown_proposal(share: Path):
    with pytest.raises(FileNotFoundError):
        await verify.verify(_config(), "nope", runner=None)


async def test_verify_rejects_traversal_ids(share: Path):
    """`..` must never reach the retention rmtree: it resolves to the library root."""
    root = share / "evolution"
    (root / "proposals").mkdir(parents=True)
    (root / "adopted" / "kept").mkdir(parents=True)
    (root / "generations").mkdir()
    (root / "EXPERIENCE.md").write_text("keep me", encoding="utf-8")

    for bad in ("..", ".", "../..", "a/b", ""):
        with pytest.raises(ValueError):
            await verify.verify(_config(), bad, runner=None)

    assert (root / "proposals").is_dir()
    assert (root / "adopted" / "kept").is_dir()
    assert (root / "EXPERIENCE.md").read_text(encoding="utf-8") == "keep me"


# ---------------------------------------------------------------------------
# Subprocess lifetime: process-group isolation + timeout group kill
# ---------------------------------------------------------------------------


class _HungProc:
    """A child that never finishes (stands in for a stalled `kimo --print`).

    The first wait() hangs until wait_for times out; after the group kill the
    reaping wait() must return promptly, or the test itself would hang.
    """

    def __init__(self) -> None:
        self.pid = 424242
        self.returncode = None
        self.killed = False
        self._waits = 0

    async def wait(self) -> int:
        self._waits += 1
        if self._waits > 1:
            self.returncode = -9
            return -9
        await asyncio.sleep(3600)
        return 0

    def kill(self) -> None:
        self.killed = True


async def test_subprocess_runner_timeout_kills_process_group(
    share: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Regression: timeout must kill the WHOLE group, not just the direct child.

    Previously `proc.kill()` orphaned agent-spawned grandchildren (long-running
    evaluation scripts) to launchd; they kept running after verify returned.
    """
    captured: dict = {}
    proc = _HungProc()

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT_S", 0.01)
    killed_groups: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed_groups.append((pid, sig)))

    record = await verify._subprocess_runner("a task", {}, tmp_path / "cwd", tmp_path / "run.out")

    if os.name != "nt":
        assert captured.get("start_new_session") is True
        assert killed_groups == [(proc.pid, signal.SIGKILL)]
        assert not proc.killed  # group kill succeeded; no single-child fallback
    assert record.note is not None and "verify_timeout" in record.note


def test_kill_process_group_falls_back_to_child(monkeypatch: pytest.MonkeyPatch):
    proc = SimpleNamespace(pid=424242, killed=False)

    def _kill() -> None:
        proc.killed = True

    proc.kill = _kill

    def _raise_lookup(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", _raise_lookup)
    verify._kill_process_group(proc)
    assert proc.killed
