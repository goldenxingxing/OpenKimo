"""Tests for the ``kimo evolve`` CLI (gating + skeleton paths, no LLM calls)."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kimo_cli.evolution import library
from kimo_cli.evolution.cli import cli


@pytest.fixture
def share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_config(share: Path, enabled: bool) -> None:
    (share / "config.toml").write_text(
        f"default_self_evolve = {'true' if enabled else 'false'}\n", encoding="utf-8"
    )


def _write_records(share: Path, task: str = "some real task") -> None:
    d = share / "sessions" / "s1" / "evolution"
    d.mkdir(parents=True)
    (d / "records.jsonl").write_text(
        json.dumps(
            {
                "outcome": "completed",
                "steps": 5,
                "input_tokens": 100,
                "output_tokens": 50,
                "reasoning_tokens": 0,
                "duration_ms": 1000,
                "task": task,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_status_always_allowed(share: Path, runner: CliRunner):
    _write_config(share, enabled=False)
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "off" in result.output


def test_propose_refused_when_switch_off(share: Path, runner: CliRunner):
    _write_config(share, enabled=False)
    result = runner.invoke(cli, ["propose"])
    assert result.exit_code == 2
    assert "Self-evolution is off" in result.output


def test_verify_refused_when_switch_off(share: Path, runner: CliRunner):
    _write_config(share, enabled=False)
    result = runner.invoke(cli, ["verify", "--proposal", "x"])
    assert result.exit_code == 2
    assert "Self-evolution is off" in result.output


def test_adopt_refused_when_switch_off(share: Path, runner: CliRunner):
    _write_config(share, enabled=False)
    result = runner.invoke(cli, ["adopt", "--proposal", "x"])
    assert result.exit_code == 2


def test_adopt_refused_without_evidence(share: Path, runner: CliRunner):
    _write_config(share, enabled=True)
    p = share / "evolution" / "proposals" / "prop1"
    p.mkdir(parents=True)
    library.save_meta(p, {"id": "prop1", "status": "proposed"})

    result = runner.invoke(cli, ["adopt", "--proposal", "prop1"])
    assert result.exit_code == 2
    assert "no verification evidence" in result.output


def test_adopt_with_evidence(share: Path, runner: CliRunner):
    _write_config(share, enabled=True)
    p = share / "evolution" / "proposals" / "prop1"
    p.mkdir(parents=True)
    library.save_meta(p, {"id": "prop1", "status": "proposed"})
    (p / "system_append.md").write_text("guidance", encoding="utf-8")
    ev = share / "evolution" / "verify_runs" / "prop1" / "evidence.json"
    ev.parent.mkdir(parents=True)
    ev.write_text("{}", encoding="utf-8")

    result = runner.invoke(cli, ["adopt", "--proposal", "prop1"])
    assert result.exit_code == 0
    assert (share / "evolution" / "adopted" / "prop1").is_dir()
