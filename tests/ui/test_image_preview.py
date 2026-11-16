"""Tests for inline terminal image preview (kimo_cli.ui.shell.image_preview)."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest

from kimo_cli.ui.shell import image_preview as ip

# A valid 1x1 transparent PNG.
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_detect_none_when_no_protocol():
    assert ip.detect_protocol({}) is None
    assert ip.detect_protocol({"TERM": "xterm-256color"}) is None


def test_detect_iterm2():
    assert ip.detect_protocol({"TERM_PROGRAM": "iTerm.app"}) == "iterm2"
    assert ip.detect_protocol({"TERM_PROGRAM": "WezTerm"}) == "iterm2"
    assert ip.detect_protocol({"TERM": "wezterm"}) == "iterm2"


def test_detect_kitty_preferred():
    assert ip.detect_protocol({"KITTY_WINDOW_ID": "1"}) == "kitty"
    assert ip.detect_protocol({"TERM": "xterm-kitty"}) == "kitty"


def test_supports_images():
    assert ip.supports_images({"TERM_PROGRAM": "iTerm.app"}) is True
    assert ip.supports_images({}) is False


def test_encode_iterm2_structure():
    data = b"hello-image"
    escape = ip.encode_iterm2(data, name="pic.png")
    assert escape.startswith("\x1b]1337;File=")
    assert "inline=1" in escape
    assert f"size={len(data)}" in escape
    assert base64.b64encode(data).decode("ascii") in escape
    assert escape.endswith("\x07")


def test_encode_kitty_requires_png():
    with pytest.raises(ValueError):
        ip.encode_kitty(b"not-a-png")


def test_encode_kitty_chunks():
    escape = ip.encode_kitty(_PNG_1PX, chunk_size=8)
    assert escape.startswith("\x1b_Ga=T,f=100,t=d")
    assert "m=0;" in escape  # last chunk terminates
    assert escape.endswith("\x1b\\")


def test_display_image_falls_back_without_protocol(tmp_path: Path):
    img = tmp_path / "a.png"
    img.write_bytes(_PNG_1PX)
    out = io.StringIO()
    assert ip.display_image(img, out=out, env={}) is False
    assert out.getvalue() == ""  # nothing written on fallback


def test_display_image_iterm2(tmp_path: Path):
    img = tmp_path / "a.png"
    img.write_bytes(_PNG_1PX)
    out = io.StringIO()
    assert ip.display_image(img, out=out, env={"TERM_PROGRAM": "iTerm.app"}) is True
    assert out.getvalue().startswith("\x1b]1337;File=")


def test_display_image_kitty(tmp_path: Path):
    img = tmp_path / "a.png"
    img.write_bytes(_PNG_1PX)
    out = io.StringIO()
    assert ip.display_image(img, out=out, env={"KITTY_WINDOW_ID": "1"}) is True
    assert "\x1b_G" in out.getvalue()


def test_display_image_missing_file_falls_back(tmp_path: Path):
    out = io.StringIO()
    assert (
        ip.display_image(tmp_path / "missing.png", out=out, env={"TERM_PROGRAM": "iTerm.app"})
        is False
    )
    assert out.getvalue() == ""
