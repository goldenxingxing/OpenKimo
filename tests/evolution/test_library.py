"""Tests for kimo_cli.evolution.library."""

import json
from pathlib import Path

import pytest

from kimo_cli.config import get_default_config
from kimo_cli.evolution import library


@pytest.fixture
def share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))
    return tmp_path


def _config(enabled: bool):
    config = get_default_config()
    config.default_self_evolve = enabled
    return config


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def test_evolution_enabled_gate():
    assert library.evolution_enabled(_config(True)) is True
    assert library.evolution_enabled(_config(False)) is False
    library.require_enabled(_config(True))
    with pytest.raises(library.EvolutionGateError):
        library.require_enabled(_config(False))


# ---------------------------------------------------------------------------
# Records aggregation
# ---------------------------------------------------------------------------


def _write_records(share: Path, session: str, lines: list[str]) -> Path:
    d = share / "sessions" / session / "evolution"
    d.mkdir(parents=True)
    f = d / "records.jsonl"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def test_iter_session_records_tolerates_old_and_corrupt_lines(share: Path):
    _write_records(
        share,
        "s1",
        [
            json.dumps(
                {
                    "ts": 1.0,
                    "mode": "agent",
                    "outcome": "completed",
                    "steps": 3,
                    "duration_ms": 100,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "reasoning_tokens": 2,
                    "task": "do the thing",
                }
            ),
            # Old-format line: no task field.
            json.dumps({"outcome": "completed", "steps": 1, "input_tokens": 1, "output_tokens": 1}),
            "not-json",
        ],
    )
    records = library.iter_session_records(share / "sessions")
    assert len(records) == 2
    assert records[0].task == "do the thing"
    assert records[0].total_tokens == 15
    assert records[1].task is None


def test_iter_session_records_missing_root(share: Path):
    assert library.iter_session_records(share / "nope") == []


def test_task_groups_failed_first(share: Path):
    def line(outcome: str, task: str, tokens: int) -> str:
        return json.dumps(
            {"outcome": outcome, "task": task, "input_tokens": tokens, "output_tokens": 0}
        )

    _write_records(
        share,
        "s1",
        [
            line("completed", "cheap task", 10),
            line("completed", "cheap task", 12),
            line("failed", "flaky task", 100),
            line("completed", "flaky task", 90),
            json.dumps({"outcome": "completed", "input_tokens": 999}),  # no task: skipped
        ],
    )
    groups = library.task_groups(library.iter_session_records(share / "sessions"))
    assert [g[0] for g in groups] == ["flaky task", "cheap task"]
    # Pattern filter
    groups = library.task_groups(library.iter_session_records(share / "sessions"), pattern="cheap")
    assert [g[0] for g in groups] == ["cheap task"]


def test_iter_all_session_records_aggregates_share_and_workdir(
    share: Path, monkeypatch: pytest.MonkeyPatch
):
    """Regression: the admin panel scanned only share App sessions and missed
    real evidence under ``<work_dir>/session-data``.  ``iter_all_session_records``
    must cover both roots."""
    _write_records(
        share,
        "app-session",
        [
            json.dumps(
                {"outcome": "completed", "task": "app task", "input_tokens": 10, "output_tokens": 5}
            )
        ],
    )
    ws_root = share / "workdir" / "session-data"
    ws_rec = ws_root / "s1" / "evolution" / "records.jsonl"
    ws_rec.parent.mkdir(parents=True)
    ws_rec.write_text(
        json.dumps(
            {"outcome": "completed", "task": "ws task", "input_tokens": 20, "output_tokens": 5}
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(library, "_local_work_dir_session_roots", lambda: [ws_root])

    records = library.iter_all_session_records()

    assert len(records) == 2
    assert {r.task for r in records} == {"app task", "ws task"}


def test_iter_all_session_records_dedupes_shared_session(
    share: Path, monkeypatch: pytest.MonkeyPatch
):
    """A session dir reachable through two roots must not be counted twice."""
    _write_records(
        share,
        "s1",
        [json.dumps({"outcome": "completed", "task": "t", "input_tokens": 1, "output_tokens": 1})],
    )
    # Both roots resolve to the same on-disk session dir.
    monkeypatch.setattr(library, "_local_work_dir_session_roots", lambda: [share / "sessions"])

    assert len(library.iter_all_session_records()) == 1


# ---------------------------------------------------------------------------
# Proposal / adopted IO and application channel
# ---------------------------------------------------------------------------


def _make_proposal(share: Path, pid: str, append: str | None, skills: dict[str, str]) -> Path:
    p = share / "evolution" / "proposals" / pid
    p.mkdir(parents=True)
    library.save_meta(p, {"id": pid, "status": "proposed"})
    if append is not None:
        (p / "system_append.md").write_text(append, encoding="utf-8")
    for name, content in skills.items():
        d = p / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(content, encoding="utf-8")
    return p


def test_list_proposals_tolerates_bad_meta(share: Path):
    _make_proposal(share, "good1", "append text", {})
    bad = share / "evolution" / "proposals" / "bad1"
    bad.mkdir(parents=True)
    (bad / "meta.json").write_text("{corrupt", encoding="utf-8")
    entries = library.list_proposals()
    # Corrupt meta must not hide a proposal: it falls back to the directory name.
    assert [e.id for e in entries] == ["bad1", "good1"]
    assert entries[0].meta == {}


@pytest.mark.parametrize(
    ("lister", "directory"),
    [(library.list_proposals, "proposals"), (library.list_adopted, "adopted")],
)
def test_list_entries_tolerates_non_object_meta(share: Path, lister, directory: str):
    entry_dir = share / "evolution" / directory / "bad-meta"
    entry_dir.mkdir(parents=True)
    (entry_dir / "meta.json").write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    entries = lister()

    assert [entry.id for entry in entries] == ["bad-meta"]
    assert entries[0].meta == {}


def test_adopt_proposal_moves_and_indexes(share: Path):
    _make_proposal(share, "prop1", "always verify before claiming done", {})
    evidence = share / "evolution" / "verify_runs" / "prop1" / "evidence.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("{}", encoding="utf-8")

    entry = library.adopt_proposal("prop1", evidence)

    assert entry.path == share / "evolution" / "adopted" / "prop1"
    assert not (share / "evolution" / "proposals" / "prop1").exists()
    assert entry.meta["status"] == "adopted"
    assert "adopted:prop1" in library.experience_file().read_text(encoding="utf-8")

    appends = library.adopted_system_appends()
    assert appends == [("prop1", "always verify before claiming done")]


def test_applied_system_append_switch_gate(share: Path, monkeypatch: pytest.MonkeyPatch):
    _make_proposal(share, "prop1", "be terse", {})
    library.adopt_proposal("prop1", None)

    # Switch off: nothing mounted.
    assert library.applied_system_append(_config(False)) is None
    assert library.applied_skill_dirs(_config(False)) == []

    # Switch on: adopted append mounted with marker comment.
    text = library.applied_system_append(_config(True))
    assert text is not None and "be terse" in text and "adopted:prop1" in text


def test_applied_channels_env_wins(share: Path, monkeypatch: pytest.MonkeyPatch):
    # Verify injection env beats the switch, both on and off.
    patch_file = share / "candidate.md"
    patch_file.write_text("candidate text", encoding="utf-8")
    monkeypatch.setenv(library.PATCH_ENV_VAR, str(patch_file))
    assert library.applied_system_append(_config(False)) == "candidate text"

    skills_root = share / "cand_skills"
    skills_root.mkdir()
    monkeypatch.setenv(library.SKILLS_ENV_VAR, str(skills_root))
    assert library.applied_skill_dirs(_config(False)) == [skills_root]

    monkeypatch.delenv(library.SKILLS_ENV_VAR)
    monkeypatch.setenv(library.SKILLS_ENV_VAR, str(share / "missing"))
    assert library.applied_skill_dirs(_config(False)) == []


def test_missing_adopted_ids_are_reported(share: Path, monkeypatch: pytest.MonkeyPatch):
    """An adopted id whose directory disappeared must not vanish silently."""
    _make_proposal(share, "prop1", "be terse", {})
    library.adopt_proposal("prop1", None)

    # The entry disappears from disk (deleted by hand, or a meta id that no
    # longer matches the directory name).
    import shutil

    shutil.rmtree(share / "evolution" / "adopted" / "prop1")

    records: list[str] = []
    from kimo_cli import logger as kimo_logger

    kimo_logger.enable("kimo_cli")
    sink_id = kimo_logger.add(lambda message: records.append(str(message)), level="WARNING")
    try:
        appends = library.adopted_system_appends()
    finally:
        kimo_logger.remove(sink_id)

    assert appends == []
    assert any("prop1" in record for record in records)


def test_push_generation_helper_is_gone(share: Path):
    """It had no callers, and restoring the call would self-deadlock on flock."""
    import inspect

    assert not hasattr(library, "_push_generation")
    assert "_push_generation" not in inspect.getsource(library.record_adoption)


def test_record_adoption_holds_the_lock_for_the_whole_read_modify_write(share: Path):
    """The comment used to name a nested helper that no longer exists."""
    import inspect

    source = inspect.getsource(library.record_adoption)
    assert "read-modify-write" in source
    assert "_push_generation" not in source


def test_list_entries_returns_every_directory(share: Path):
    """The ``is not None`` filter could never drop anything."""
    import inspect

    source = inspect.getsource(library.list_entries)
    assert "if e is not None" not in source

    proposals = share / "evolution" / "proposals"
    (proposals / "with-meta").mkdir(parents=True)
    (proposals / "without-meta").mkdir(parents=True)
    (proposals / "with-meta" / "meta.json").write_text(
        json.dumps({"id": "with-meta"}), encoding="utf-8"
    )

    entries = library.list_entries(proposals)

    assert {entry.id for entry in entries} == {"with-meta", "without-meta"}
