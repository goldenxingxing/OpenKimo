"""The desktop Settings write path for the auto-compaction ratio.

The value ends up in kimi-cli's `config.toml`, so the thing worth pinning is
that what Settings writes is exactly what kimi-cli can load back.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

from app_main.configtoml import set_compaction_ratio  # noqa: E402


def _config(tmp_path: Path, body: str = "[loop_control]\nmax_steps_per_turn = 200\n") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_section_is_created_when_absent(tmp_path: Path) -> None:
    path = _config(tmp_path, "default_thinking = true\n")

    set_compaction_ratio(path, 0.8)

    text = path.read_text(encoding="utf-8")
    assert "[loop_control]" in text
    assert tomllib.loads(text)["loop_control"]["compaction_trigger_ratio"] == 0.8
    # Unrelated settings survive.
    assert tomllib.loads(text)["default_thinking"] is True


def test_a_neighbouring_key_in_the_same_section_survives(tmp_path: Path) -> None:
    path = _config(tmp_path)

    set_compaction_ratio(path, 0.8)

    loaded = tomllib.loads(path.read_text(encoding="utf-8"))
    assert loaded["loop_control"]["max_steps_per_turn"] == 200
    assert loaded["loop_control"]["compaction_trigger_ratio"] == 0.8


def test_writing_the_same_value_leaves_the_file_untouched(tmp_path: Path) -> None:
    path = _config(tmp_path)
    set_compaction_ratio(path, 0.7)
    before = path.read_text(encoding="utf-8")

    set_compaction_ratio(path, 0.7)

    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("ratio", [0.49, 0.0, 1.0, 1.5, -0.2])
def test_an_out_of_range_ratio_is_refused_rather_than_written(
    tmp_path: Path, ratio: float
) -> None:
    """kimi-cli rejects these on load; failing here names the real culprit."""
    path = _config(tmp_path)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="0.5-0.99"):
        set_compaction_ratio(path, ratio)

    assert path.read_text(encoding="utf-8") == before


def test_a_missing_config_file_is_a_no_op(tmp_path: Path) -> None:
    missing = tmp_path / "absent.toml"

    set_compaction_ratio(missing, 0.8)

    assert not missing.exists()


def test_unparseable_toml_is_left_alone(tmp_path: Path) -> None:
    path = _config(tmp_path, "this is not = = toml\n")
    before = path.read_text(encoding="utf-8")

    set_compaction_ratio(path, 0.8)

    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("ratio", [0.5, 0.7, 0.95, 0.99])
def test_what_settings_writes_is_what_kimi_cli_loads(tmp_path: Path, ratio: float) -> None:
    """The contract that matters: no value Settings accepts can break startup."""
    sys.path.insert(0, str(ROOT / "kimi-cli" / "src"))
    from kimi_cli.config import load_config_from_string

    path = _config(tmp_path)
    set_compaction_ratio(path, ratio)

    loaded = load_config_from_string(json.dumps(tomllib.loads(path.read_text(encoding="utf-8"))))

    assert loaded.loop_control.compaction_trigger_ratio == ratio


# ---------------------------------------------------------------------------
# Reading the current value back for the Settings control
# ---------------------------------------------------------------------------


def test_the_default_is_used_when_nothing_is_configured(tmp_path: Path) -> None:
    from app_main.configtoml import DEFAULT_COMPACTION_RATIO, get_compaction_ratio

    assert get_compaction_ratio(tmp_path / "absent.toml") == DEFAULT_COMPACTION_RATIO
    assert get_compaction_ratio(_config(tmp_path)) == DEFAULT_COMPACTION_RATIO


def test_a_written_value_reads_back(tmp_path: Path) -> None:
    from app_main.configtoml import get_compaction_ratio

    path = _config(tmp_path)
    set_compaction_ratio(path, 0.72)

    assert get_compaction_ratio(path) == 0.72


@pytest.mark.parametrize("body", ["[loop_control]\ncompaction_trigger_ratio = 2.5\n", "== bad ==\n"])
def test_an_unusable_file_falls_back_rather_than_crashing_settings(
    tmp_path: Path, body: str
) -> None:
    from app_main.configtoml import DEFAULT_COMPACTION_RATIO, get_compaction_ratio

    assert get_compaction_ratio(_config(tmp_path, body)) == DEFAULT_COMPACTION_RATIO


def test_the_slider_range_matches_what_kimi_cli_accepts() -> None:
    """A reachable slider position must never produce an unloadable config."""
    source = (ROOT / "packaging" / "app_main" / "settings_window.py").read_text(encoding="utf-8")

    assert "setMinValue_(0.5)" in source
    assert "setMaxValue_(0.99)" in source
    # Spliced into the vertical chain, and written on save.
    assert "fr_compaction.bottomAnchor()" in source
    assert "configtoml.set_compaction_ratio(" in source
