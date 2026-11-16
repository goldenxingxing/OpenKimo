"""Tests for custom theme loading (kimo_cli.ui.theme)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from prompt_toolkit.styles import Style as PTKStyle
from rich.style import Style as RichStyle

from kimo_cli.ui import theme as th


@pytest.fixture(autouse=True)
def _clear_custom():
    th.clear_custom_theme()
    th.set_active_theme("dark")
    yield
    th.clear_custom_theme()
    th.set_active_theme("dark")


def _triplet(style: RichStyle) -> tuple[int, int, int] | None:
    color = style.bgcolor
    if color is None:
        return None
    triplet = color.triplet
    return (triplet.red, triplet.green, triplet.blue)


def test_load_toml(tmp_path: Path):
    file = tmp_path / "my.toml"
    file.write_text(
        'name = "ocean"\nbase = "dark"\n[diff]\nadd_bg = "#123456"\n[toolbar]\ncwd = "fg:#abcdef"\n',
        encoding="utf-8",
    )
    custom = th.load_custom_theme(file)
    assert custom.name == "ocean"
    assert custom.base == "dark"
    assert custom.diff["add_bg"] == "#123456"
    assert custom.toolbar["cwd"] == "fg:#abcdef"


def test_load_json(tmp_path: Path):
    file = tmp_path / "my.json"
    file.write_text(
        json.dumps({"base": "light", "mcp": {"connected": "fg:#00ff00"}}), encoding="utf-8"
    )
    custom = th.load_custom_theme(file)
    assert custom.base == "light"
    assert custom.mcp["connected"] == "fg:#00ff00"
    assert custom.name == "my"  # falls back to file stem


def test_invalid_base_raises(tmp_path: Path):
    file = tmp_path / "bad.toml"
    file.write_text('base = "blue"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        th.load_custom_theme(file)


def test_no_custom_theme_is_unchanged():
    assert th.get_custom_theme() is None
    assert th.get_diff_colors() == th._DIFF_DARK
    assert th.get_toolbar_colors() == th._TOOLBAR_DARK


def test_diff_override_applied():
    th.set_custom_theme(th.CustomTheme(name="x", diff={"add_bg": "#ff0000"}))
    assert _triplet(th.get_diff_colors().add_bg) == (255, 0, 0)
    # Unset field inherits base
    assert th.get_diff_colors().del_bg == th._DIFF_DARK.del_bg


def test_toolbar_override_applied():
    th.set_custom_theme(th.CustomTheme(name="x", toolbar={"cwd": "fg:#ff0000"}))
    assert th.get_toolbar_colors().cwd == "fg:#ff0000"
    assert th.get_toolbar_colors().tip == th._TOOLBAR_DARK.tip


def test_mcp_override_applied():
    th.set_custom_theme(th.CustomTheme(name="x", mcp={"connected": "fg:#00ff00"}))
    assert th.get_mcp_prompt_colors().connected == "fg:#00ff00"


def test_custom_base_switches_palette():
    th.set_custom_theme(th.CustomTheme(name="x", base="light", toolbar={}))
    # No overrides, but base=light means the light palette is used.
    assert th.get_toolbar_colors() == th._TOOLBAR_LIGHT


def test_clear_reverts():
    th.set_custom_theme(th.CustomTheme(name="x", toolbar={"cwd": "fg:#ff0000"}))
    th.clear_custom_theme()
    assert th.get_toolbar_colors() == th._TOOLBAR_DARK


def test_prompt_and_task_browser_return_styles():
    th.set_custom_theme(
        th.CustomTheme(
            name="x",
            prompt={"bottom-toolbar": "fg:#123456"},
            task_browser={"header": "bg:#000000"},
        )
    )
    assert isinstance(th.get_prompt_style(), PTKStyle)
    assert isinstance(th.get_task_browser_style(), PTKStyle)
