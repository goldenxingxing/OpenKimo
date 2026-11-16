"""Tests for kimo_cli.evolution.auto_evolve."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kimo_cli.config import get_default_config
from kimo_cli.evolution import auto_evolve


@pytest.fixture
def share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate KIMO_SHARE_DIR per test."""
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))
    return tmp_path


def _config(
    enabled: bool = False,
    min_evidence: int = 3,
    cooldown_s: int = 0,
):
    config = get_default_config()
    config.default_self_evolve = enabled
    config.evolution_auto_min_evidence = min_evidence
    config.evolution_auto_cooldown_s = cooldown_s
    return config


def _write_turn(path: Path, *, outcome: str = "completed", task: str = "t"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": 1.0,
                    "mode": "agent",
                    "outcome": outcome,
                    "steps": 1,
                    "duration_ms": 100,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "reasoning_tokens": 0,
                    "task": task,
                }
            )
            + "\n"
        )


def _seed_workspace_records(work_dir: Path, count: int, task: str = "hello") -> None:
    """Write `count` completed turn records into work_dir/session-data/<sid>/evolution/."""
    d = work_dir / "session-data" / "s1" / "evolution"
    d.mkdir(parents=True, exist_ok=True)
    rec = d / "records.jsonl"
    for _ in range(count):
        _write_turn(rec, task=task)


def _seed_share_records(share: Path, count: int, task: str = "share-task") -> None:
    d = share / "sessions" / "app-session" / "evolution"
    d.mkdir(parents=True, exist_ok=True)
    rec = d / "records.jsonl"
    for _ in range(count):
        _write_turn(rec, task=task)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_skip_switch_off(share: Path):
    async def _go():
        return await auto_evolve.auto_evolve_maybe(_config(enabled=False))

    r = asyncio.run(_go())
    assert r.attempted is False
    assert r.skip_reason == "switch_off"
    assert r.error is None


def test_skip_subprocess_env(share: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KIMO_EVOLUTION_SUBPROCESS", "1")

    async def _go():
        return await auto_evolve.auto_evolve_maybe(_config(enabled=True))

    r = asyncio.run(_go())
    assert r.attempted is False
    assert r.skip_reason == "subprocess"


def test_skip_insufficient_evidence_workspace(share: Path, tmp_path: Path):
    # Workspace has fewer than min_evidence=3 completed turns.
    _seed_workspace_records(tmp_path, 2)

    async def _go():
        return await auto_evolve.auto_evolve_maybe(
            _config(enabled=True, min_evidence=3), work_dir=tmp_path
        )

    r = asyncio.run(_go())
    assert r.attempted is False
    assert "insufficient_evidence" in (r.skip_reason or "")
    assert "2 completed turns" in (r.skip_reason or "")


def test_skip_insufficient_evidence_share_only(share: Path):
    # No records anywhere — share-only default.
    async def _go():
        return await auto_evolve.auto_evolve_maybe(
            _config(enabled=True, min_evidence=3), work_dir=None
        )

    r = asyncio.run(_go())
    assert r.attempted is False
    assert "insufficient_evidence" in (r.skip_reason or "")
    assert "0 completed turns" in (r.skip_reason or "")


def test_skip_cooldown(share: Path, tmp_path: Path):
    # Seed enough evidence so the gate would otherwise pass.
    _seed_workspace_records(tmp_path, 5)
    # Pre-seed the state file to just-now.
    sp = auto_evolve._state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    import time

    auto_evolve._save_state({"last_cycle_end": time.time()})

    async def _go():
        return await auto_evolve.auto_evolve_maybe(
            _config(enabled=True, cooldown_s=120), work_dir=tmp_path
        )

    r = asyncio.run(_go())
    assert r.attempted is False
    assert "cooldown" in (r.skip_reason or "")
    assert "remaining" in (r.skip_reason or "")


def test_state_persisted_on_switch_off_skip(share: Path):
    """Regression for the persistence gap AND the starvation bug.

    Persistence (bug B): the diagnostic fields ``last_dispatch_ts`` /
    ``last_skip_reason`` MUST be saved on skip paths so the admin panel
    can see why a cycle did not run. An early-return branch that forgot
    to call ``_save_state`` would lose this.

    Starvation (critical bug): ``last_cycle_end`` must NOT be advanced
    on skip paths. Setting it on every dispatch (including cooldown /
    switch_off / subprocess) would reopen the cooldown window after
    every turn-end, permanently starving auto_evolve after the first
    completed cycle.
    """

    async def _go():
        return await auto_evolve.auto_evolve_maybe(_config(enabled=False))

    r = asyncio.run(_go())
    assert r.skip_reason == "switch_off"
    state = auto_evolve.get_state()
    assert state.get("last_skip_reason") == "switch_off"
    assert state.get("last_dispatch_ts", 0) > 0
    # last_cycle_end must be ABSENT on skip paths — that is the fix.
    assert "last_cycle_end" not in state


def test_dispatch_from_schedule_not_cycle_in_flight(
    share: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Regression: ``auto_evolve_maybe`` must not self-block via ``_is_active``.

    ``schedule_auto_evolve`` adds its own task to ``_ACTIVE_TASKS`` before
    the task body runs. An internal ``if _is_active(): return`` guard inside
    ``auto_evolve_maybe`` therefore always tripped — every scheduled cycle
    came back with ``skip_reason="cycle_in_flight"`` without ever running.
    That guard has been removed from the function body; the outer
    ``schedule_auto_evolve`` still blocks a second task from being created.

    We stub ``editor.propose`` to raise so the cycle exits quickly without
    touching the network.
    """
    _seed_workspace_records(tmp_path, 5)

    async def _fake_propose(*args, **kwargs):
        raise RuntimeError("stub: no LLM in unit tests")

    monkeypatch.setattr(auto_evolve.editor, "propose", _fake_propose)

    async def _go():
        # Simulate schedule_auto_evolve: mark this coroutine as an active
        # task so _is_active() would return True if the guard were still
        # inside auto_evolve_maybe.
        current = asyncio.current_task()
        auto_evolve._ACTIVE_TASKS.add(current)
        try:
            return await auto_evolve.auto_evolve_maybe(
                _config(enabled=True, min_evidence=3), work_dir=tmp_path
            )
        finally:
            auto_evolve._ACTIVE_TASKS.discard(current)

    r = asyncio.run(_go())
    # The critical regression check: must NOT have skipped with
    # cycle_in_flight — that was the bug.
    assert r.skip_reason != "cycle_in_flight"
    # With editor.propose stubbed to raise, the outer try catches the
    # exception and sets ``result.error`` on the outer local.  We assert
    # that we got past the gate checks rather than being blocked at the
    # _is_active() guard.
    assert r.error is not None
    assert "RuntimeError" in r.error


# ---------------------------------------------------------------------------
# Record aggregation
# ---------------------------------------------------------------------------


def test_iter_all_records_finds_workspace_records(share: Path, tmp_path: Path):
    # share/ has 0 records, workspace has 2 — expect 2 total.
    _seed_workspace_records(tmp_path, 2, task="ws-task")
    records = auto_evolve._iter_all_records(tmp_path)
    assert len(records) == 2
    assert all(r.session_dir == tmp_path / "session-data" / "s1" for r in records)


def test_iter_all_records_finds_share_records(share: Path):
    _seed_share_records(share, 3)
    records = auto_evolve._iter_all_records(None)
    assert len(records) == 3
    assert all("app-session" in str(r.session_dir) for r in records)


def test_iter_all_records_dedupes_and_merges(share: Path, tmp_path: Path):
    # Both roots with distinct session dirs: expect 3 + 2 = 5.
    _seed_share_records(share, 3, task="share")
    _seed_workspace_records(tmp_path, 2, task="workspace")
    records = auto_evolve._iter_all_records(tmp_path)
    assert len(records) == 5
    # Session dirs are unique.
    dirs = {r.session_dir for r in records}
    assert len(dirs) == 2


def test_iter_all_records_ignores_missing_roots(share: Path, tmp_path: Path):
    # No records anywhere — no crash.
    records = auto_evolve._iter_all_records(tmp_path)
    assert records == []
    records_none = auto_evolve._iter_all_records(None)
    assert records_none == []


def test_iter_all_records_accepts_kaospath(share: Path, tmp_path: Path):
    """Regression: ``session.work_dir`` is a KaosPath, which has no ``rglob``
    and an async ``is_dir``.  ``_iter_all_records`` used to pass it straight
    to ``library.iter_session_records``, crashing with

        AttributeError: 'KaosPath' object has no attribute 'rglob'

    (observed in auto_evolve_state.json last_error).  It must normalise to a
    local Path first."""
    from kaos.path import KaosPath

    _seed_workspace_records(tmp_path, 2, task="kaos-task")
    records = auto_evolve._iter_all_records(KaosPath(str(tmp_path)))
    assert len(records) == 2
    assert all(r.task == "kaos-task" for r in records)


# ---------------------------------------------------------------------------
# Cooldown / min_evidence accessors
# ---------------------------------------------------------------------------


def test_accessors_clamp_to_sane_values(share: Path):
    cfg = _config(min_evidence=-5, cooldown_s=-100)
    assert auto_evolve._min_evidence(cfg) == 1
    assert auto_evolve._cooldown_s(cfg) == 0

    cfg = _config(min_evidence=10, cooldown_s=60)
    assert auto_evolve._min_evidence(cfg) == 10
    assert auto_evolve._cooldown_s(cfg) == 60


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------


def test_state_file_roundtrip(share: Path):
    sp = auto_evolve._state_path()
    assert not sp.exists()
    auto_evolve._save_state({"foo": "bar"})
    assert sp.exists()
    assert auto_evolve._load_state() == {"foo": "bar"}


def test_state_file_tolerates_corruption(share: Path):
    sp = auto_evolve._state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("not json", encoding="utf-8")
    assert auto_evolve._load_state() == {}


# ---------------------------------------------------------------------------
# Diagnostic helpers
# ---------------------------------------------------------------------------


def test_get_state_returns_dict(share: Path):
    assert isinstance(auto_evolve.get_state(), dict)


def test_active_task_count_initially_zero():
    assert auto_evolve.active_task_count() == 0
