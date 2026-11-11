"""Tests for the /feedback shell slash command.

The kimo-code feedback endpoint was removed with the Moonshot decoupling, so
/feedback now only opens the GitHub issue tracker. These tests pin that
behaviour: which URL is opened, and that a browser that refuses to open still
tells the user where to go.
"""

from __future__ import annotations

from unittest.mock import Mock

from kimo_cli.ui.shell import slash as shell_slash

ISSUE_URL = "https://github.com/goldenxingxing/OpenKimo/issues"


def test_feedback_command_registered_in_shell_registries() -> None:
    assert shell_slash.registry.find_command("feedback") is not None
    assert shell_slash.shell_mode_registry.find_command("feedback") is not None


async def test_feedback_opens_the_issue_tracker(monkeypatch) -> None:
    open_mock = Mock(return_value=True)
    monkeypatch.setattr("webbrowser.open", open_mock)

    await shell_slash.feedback(None, "")  # type: ignore[arg-type]

    open_mock.assert_called_once()
    assert open_mock.call_args.args[0] == ISSUE_URL


async def test_feedback_prints_fallback_when_browser_cannot_open(monkeypatch) -> None:
    open_mock = Mock(return_value=False)
    monkeypatch.setattr("webbrowser.open", open_mock)
    print_mock = Mock()
    monkeypatch.setattr(shell_slash.console, "print", print_mock)

    await shell_slash.feedback(None, "")  # type: ignore[arg-type]

    print_mock.assert_called_once()
    assert ISSUE_URL in str(print_mock.call_args.args[0])
