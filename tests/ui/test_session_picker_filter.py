"""Tests for the session-picker text filter (kimo_cli.ui.shell.session_picker)."""

from __future__ import annotations

from types import SimpleNamespace

from kimo_cli.ui.shell.session_picker import filter_sessions


def _session(title: str, session_id: str, work_dir: str) -> SimpleNamespace:
    return SimpleNamespace(title=title, id=session_id, work_dir=work_dir)


_SESSIONS = [
    _session("Fix login bug", "abc123def", "/home/user/proj"),
    _session("Write docs", "zzz999", "/home/user/other"),
    _session("Refactor parser", "mno456", "/home/user/proj"),
]


def test_empty_query_returns_all():
    assert len(filter_sessions(_SESSIONS, "")) == 3
    assert len(filter_sessions(_SESSIONS, "   ")) == 3


def test_filter_by_title_case_insensitive():
    result = filter_sessions(_SESSIONS, "LOGIN")
    assert len(result) == 1
    assert result[0].id == "abc123def"


def test_filter_by_id():
    result = filter_sessions(_SESSIONS, "zzz999")
    assert len(result) == 1
    assert result[0].title == "Write docs"


def test_filter_by_work_dir():
    result = filter_sessions(_SESSIONS, "other")
    assert len(result) == 1
    assert result[0].id == "zzz999"


def test_filter_multiple_matches():
    result = filter_sessions(_SESSIONS, "proj")
    assert len(result) == 2


def test_filter_no_match():
    assert filter_sessions(_SESSIONS, "nonexistent-xyz") == []
