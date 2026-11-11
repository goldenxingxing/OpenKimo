"""Tests for the self-evolution evidence recorder (soul/evolution.py)."""

import json
from pathlib import Path

from kimo_cli.soul.evolution import EvolutionRecorder, EvolutionTurnRecord


def _record(**overrides) -> EvolutionTurnRecord:
    defaults: dict = dict(
        ts=1.0,
        mode="agent",
        outcome="completed",
        steps=3,
        duration_ms=100,
        input_tokens=10,
        output_tokens=5,
        reasoning_tokens=2,
    )
    defaults.update(overrides)
    return EvolutionTurnRecord(**defaults)


def test_record_turn_appends_jsonl(tmp_path: Path):
    recorder = EvolutionRecorder(tmp_path)
    recorder.record_turn(_record())
    recorder.record_turn(_record(ts=2.0, outcome="cancelled", steps=0, input_tokens=0))

    lines = (tmp_path / "evolution" / "records.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["mode"] == "agent"
    assert first["input_tokens"] == 10
    assert first["reasoning_tokens"] == 2
    assert json.loads(lines[1])["outcome"] == "cancelled"


def test_record_turn_swallows_os_errors(tmp_path: Path):
    """A blocked evolution dir must not propagate: evolution never breaks a turn."""
    (tmp_path / "evolution").write_text("occupied", encoding="utf-8")

    recorder = EvolutionRecorder(tmp_path)
    recorder.record_turn(_record())  # must not raise

    assert not (tmp_path / "evolution" / "records.jsonl").exists()
