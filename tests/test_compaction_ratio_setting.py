"""The desktop Settings write path for the auto-compaction ratio.

The value ends up in kimo-cli's `config.toml`, so the thing worth pinning is
that what Settings writes is exactly what kimo-cli can load back.
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
    """kimo-cli rejects these on load; failing here names the real culprit."""
    path = _config(tmp_path)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="0.5-0.99"):
        set_compaction_ratio(path, ratio)

    assert path.read_text(encoding="utf-8") == before


def test_a_missing_config_file_is_created(tmp_path: Path) -> None:
    """First run has no config.toml yet, and the slider must still stick.

    This used to be a no-op, copied from set_default_model. That is harmless
    for providers — the env merge re-seeds them from LLM_PROVIDERS on the next
    server start — but compaction_trigger_ratio has no .env counterpart, so the
    user's change was discarded silently and reverted on the next open. The
    Settings window opens before the server has ever run on a fresh install,
    which is exactly when it matters.

    Safe because kimo-cli tolerates a partial config: load_config() on a file
    holding only [loop_control] returns models={} and default_model='' without
    raising.
    """
    missing = tmp_path / "absent.toml"

    set_compaction_ratio(missing, 0.8)

    assert missing.exists()
    assert "compaction_trigger_ratio = 0.8" in missing.read_text(encoding="utf-8")


def test_an_out_of_range_value_is_refused_even_without_a_file(tmp_path: Path) -> None:
    """The range check used to sit after the existence check.

    So set_compaction_ratio(missing, 5.0) returned quietly instead of
    refusing — the "rejected rather than written" contract held only when the
    file already existed.
    """
    missing = tmp_path / "absent.toml"

    with pytest.raises(ValueError, match="0.5-0.99"):
        set_compaction_ratio(missing, 5.0)

    assert not missing.exists()


def test_a_non_numeric_existing_value_is_replaced(tmp_path: Path) -> None:
    """float() on it raised a ValueError without the "0.5-0.99" text.

    settings_window's `except Exception: log.exception(...)` then swallowed it
    and the user's slider change vanished with no message.
    """
    path = _config(tmp_path, '[loop_control]\ncompaction_trigger_ratio = "high"\n')

    set_compaction_ratio(path, 0.8)

    assert "compaction_trigger_ratio = 0.8" in path.read_text(encoding="utf-8")


def test_unparseable_toml_is_left_alone(tmp_path: Path) -> None:
    path = _config(tmp_path, "this is not = = toml\n")
    before = path.read_text(encoding="utf-8")

    set_compaction_ratio(path, 0.8)

    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("ratio", [0.5, 0.7, 0.95, 0.99])
def test_what_settings_writes_is_what_kimo_cli_loads(tmp_path: Path, ratio: float) -> None:
    """The contract that matters: no value Settings accepts can break startup."""
    sys.path.insert(0, str(ROOT / "src"))
    from kimo_cli.config import load_config_from_string

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


def test_the_slider_range_matches_what_kimo_cli_accepts() -> None:
    """A reachable slider position must never produce an unloadable config."""
    source = (ROOT / "packaging" / "app_main" / "settings_window.py").read_text(encoding="utf-8")

    assert "setMinValue_(0.5)" in source
    assert "setMaxValue_(0.99)" in source
    # Spliced into the vertical chain, and written on save.
    assert "fr_compaction.bottomAnchor()" in source
    assert "configtoml.set_compaction_ratio(" in source


# ---------------------------------------------------------------------------
# Slider readouts
# ---------------------------------------------------------------------------


def _settings_source() -> str:
    return (ROOT / "packaging" / "app_main" / "settings_window.py").read_text(encoding="utf-8")


def _extract(name: str) -> object:
    """Evaluate one pure formatter without importing AppKit."""
    import re

    match = re.search(rf"def {name}\(value: float\) -> str:\n(?:    .*\n)+", _settings_source())
    assert match is not None, f"{name} not found"
    namespace: dict[str, object] = {}
    exec(match.group(0), namespace)  # noqa: S102
    return namespace[name]


@pytest.mark.parametrize(
    ("value", "shown"),
    [(0.5, "50%"), (0.72, "72%"), (0.8, "80%"), (0.95, "95%"), (0.99, "99%")],
)
def test_the_ratio_reads_out_as_a_percentage(value: float, shown: str) -> None:
    assert _extract("_format_ratio")(value) == shown  # type: ignore[operator]


@pytest.mark.parametrize(("value", "shown"), [(0.0, "0.00"), (0.7, "0.70"), (2.0, "2.00")])
def test_the_temperature_reads_out_to_two_decimals(value: float, shown: str) -> None:
    assert _extract("_format_temperature")(value) == shown  # type: ignore[operator]


def test_both_sliders_report_their_value_while_dragging() -> None:
    """A bare slider gives no way to tell 0.72 from 0.78."""
    source = _settings_source()

    for slider in ("temp", "compaction"):
        assert f"{slider}.setContinuous_(True)" in source
        assert f"{slider}.setTarget_(controller)" in source
    assert "def onTemperatureChanged_(self, sender)" in source
    assert "def onCompactionChanged_(self, sender)" in source
    # The initial readout goes through the same formatter as the live update,
    # so the value shown on open cannot drift from the value shown on drag.
    assert "_label(_format_ratio(compaction_val))" in source
    assert "_label(_format_temperature(temp_val))" in source
