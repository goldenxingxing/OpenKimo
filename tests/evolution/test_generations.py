"""Tests for the generation (version management) model in evolution.library."""

import json
from pathlib import Path

import pytest

from kimo_cli.evolution import library


@pytest.fixture
def share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))
    return tmp_path


def _make_proposal(share: Path, pid: str) -> Path:
    p = share / "evolution" / "proposals" / pid
    p.mkdir(parents=True)
    library.save_meta(p, {"id": pid, "status": "proposed", "created_at": 1.0})
    (p / "system_append.md").write_text(f"guidance {pid}", encoding="utf-8")
    return p


def _adopt(share: Path, pid: str) -> None:
    _make_proposal(share, pid)
    library.adopt_proposal(pid, None)


# ---------------------------------------------------------------------------
# Migration & basics
# ---------------------------------------------------------------------------


def test_migration_builds_gen1_from_adopted(share: Path, monkeypatch: pytest.MonkeyPatch):
    # Adopted entries created while generations.json does not exist yet.
    monkeypatch.setattr(library, "record_adoption", lambda _id: 1)  # suppress gen writes
    _adopt(share, "old1")
    _adopt(share, "old2")
    (share / "evolution" / "generations.json").unlink(missing_ok=True)

    state = library.load_generations()
    assert state["current"] == 1
    assert state["generations"][0]["adopted_ids"] == ["old1", "old2"]
    assert library.current_adopted_ids() == ["old1", "old2"]


@pytest.mark.parametrize(
    "bad_state",
    [
        [],
        {"generations": {}},
        {"generations": [None], "current": 1},
        {"generations": [{"adopted_ids": "a1"}], "current": 1},
        {"generations": [], "current": []},
        {"generations": [], "current": 1},
        {"generations": [], "disabled_ids": {}},
    ],
)
def test_load_generations_rebuilds_invalid_state(share: Path, bad_state):
    adopted = share / "evolution" / "adopted" / "a1"
    adopted.mkdir(parents=True)
    library.save_meta(adopted, {"id": "a1"})
    library.generations_file().write_text(json.dumps(bad_state), encoding="utf-8")

    state = library.load_generations()

    assert state["current"] == 1
    assert state["generations"][0]["adopted_ids"] == ["a1"]
    assert library.current_adopted_ids() == ["a1"]


def test_record_adoption_accumulates_generations(share: Path):
    _adopt(share, "a1")
    assert library.current_generation_n() == 1
    _adopt(share, "a2")
    assert library.current_generation_n() == 2
    assert library.current_adopted_ids() == ["a1", "a2"]
    _adopt(share, "a3")
    state = library.load_generations()
    assert state["current"] == 3
    assert len(state["generations"]) == 3


# ---------------------------------------------------------------------------
# Rollback / advance
# ---------------------------------------------------------------------------


def test_rollback_and_advance(share: Path):
    _adopt(share, "a1")
    _adopt(share, "a2")
    _adopt(share, "a3")

    assert library.rollback_generation() == 2
    assert library.current_adopted_ids() == ["a1", "a2"]
    assert library.rollback_generation() == 1
    assert library.current_adopted_ids() == ["a1"]
    # Advance back up: entries are never deleted.
    assert library.advance_generation() == 2
    assert library.advance_generation() == 3
    assert library.current_adopted_ids() == ["a1", "a2", "a3"]


def test_rollback_at_first_generation_fails(share: Path):
    _adopt(share, "a1")
    with pytest.raises(ValueError):
        library.rollback_generation()


def test_advance_at_newest_fails(share: Path):
    _adopt(share, "a1")
    with pytest.raises(ValueError):
        library.advance_generation()


def test_application_channel_follows_generation(share: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(library.PATCH_ENV_VAR, raising=False)
    from kimo_cli.config import get_default_config

    config = get_default_config()
    config.default_self_evolve = True

    _adopt(share, "a1")
    _adopt(share, "a2")
    texts = library.applied_system_append(config)
    assert texts is not None and "a1" in texts and "a2" in texts

    library.rollback_generation()
    texts = library.applied_system_append(config)
    assert texts is not None and "a1" in texts and "a2" not in texts


# ---------------------------------------------------------------------------
# Disabled overlay
# ---------------------------------------------------------------------------


def test_disabled_overlay_filters_current_set(share: Path):
    _adopt(share, "a1")
    _adopt(share, "a2")
    library.set_adopted_enabled("a1", enabled=False)
    assert library.current_adopted_ids() == ["a2"]
    assert library.disabled_ids() == ["a1"]
    library.set_adopted_enabled("a1", enabled=True)
    assert library.current_adopted_ids() == ["a1", "a2"]


def test_disabled_survives_rollback_cycle(share: Path):
    _adopt(share, "a1")
    _adopt(share, "a2")
    library.set_adopted_enabled("a2", enabled=False)
    library.rollback_generation()
    assert library.current_adopted_ids() == ["a1"]
    library.advance_generation()
    # Overlay is independent of generation history.
    assert library.current_adopted_ids() == ["a1"]


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_log_and_read_actions(share: Path):
    library.log_action("switch", "tester", {"enabled": True})
    library.log_action("adopt", "tester", {"proposal_id": "p1"})
    actions = library.read_actions(limit=10)
    assert len(actions) == 2
    # Most recent first
    assert actions[0]["action"] == "adopt"
    assert actions[1]["action"] == "switch"
    assert actions[1]["detail"]["enabled"] is True


def test_read_actions_tolerates_bad_lines(share: Path):
    library.log_action("a", "x")
    with open(share / "evolution" / "actions.jsonl", "a", encoding="utf-8") as f:
        f.write("{broken\n")
    actions = library.read_actions()
    assert len(actions) == 1
