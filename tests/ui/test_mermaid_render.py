"""Tests for Mermaid rendering (kimo_cli.ui.shell.mermaid_render)."""

from __future__ import annotations

import pytest

from kimo_cli.ui.shell import mermaid_render as mr


def test_empty_code_raises():
    with pytest.raises(mr.MermaidRenderError):
        mr.render_mermaid_to_png("   ")


def test_render_raises_when_mmdc_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mr, "find_mmdc", lambda: None)
    with pytest.raises(mr.MermaidRenderError) as exc:
        mr.render_mermaid_to_png("graph TD; A-->B")
    assert "mermaid-cli" in str(exc.value)


def test_inline_falls_back_when_mmdc_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mr, "find_mmdc", lambda: None)
    assert mr.render_mermaid_inline("graph TD; A-->B") is False


def test_mmdc_available(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mr.shutil, "which", lambda _name: None)
    assert mr.mmdc_available() is False
    monkeypatch.setattr(mr.shutil, "which", lambda _name: "/usr/bin/mmdc")
    assert mr.mmdc_available() is True
    assert mr.find_mmdc() == "/usr/bin/mmdc"
