"""Settings changes reach a running server without ending anyone's session.

The desktop app used to have exactly one way to make a setting take effect:
restart the server. Adding a provider therefore killed whatever session was
mid-turn. The replacement is a request the app leaves for the server to find,
and the two halves live in different packages -- so what is worth pinning is
that they still agree on where that request goes, and that the only thing left
that restarts anything is the port.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

from app_main.server import RELOAD_SIGNAL_NAME, request_config_reload  # noqa: E402

MAC_SETTINGS = (ROOT / "packaging" / "app_main" / "settings_window.py").read_text(encoding="utf-8")
WIN_SETTINGS = (ROOT / "packaging" / "app_main_win" / "settings_window.py").read_text(
    encoding="utf-8"
)
SETTINGS_HTML = (ROOT / "packaging" / "app_main_win" / "settings.html").read_text(encoding="utf-8")


def test_the_request_lands_where_the_server_looks_for_it(tmp_path, monkeypatch) -> None:
    """The one contract spanning `packaging` and `kimo_cli`.

    Nothing else connects them: the app writes a file, the server polls it. A
    rename on either side would silently mean settings stop taking effect, with
    no error anywhere.
    """
    from kimo_cli.web.config_reload import reload_signal_path

    assert request_config_reload(SimpleNamespace(app_support=tmp_path)) is True

    monkeypatch.setenv("OPENKIMO_APP_DATA_DIR", str(tmp_path))
    found = reload_signal_path()
    assert found is not None
    assert found.exists()


def test_two_saves_in_the_same_second_are_two_requests(tmp_path) -> None:
    """Detection is by content as well as mtime, which has one-second grain."""
    paths = SimpleNamespace(app_support=tmp_path)
    request_config_reload(paths)
    # The writer owns the name; spelling it out here means a rename passes
    # this test while settings quietly stop taking effect.
    signal = tmp_path / RELOAD_SIGNAL_NAME
    first = signal.read_text(encoding="utf-8")
    request_config_reload(paths)

    assert signal.read_text(encoding="utf-8") != first


def test_the_server_is_told_which_env_file_to_re_read() -> None:
    """Its own environment is frozen at spawn; without this it cannot refresh."""
    supervisor = (ROOT / "packaging" / "app_main" / "server.py").read_text(encoding="utf-8")

    assert 'env["OPENKIMO_ENV_FILE"] = str(p.env_file)' in supervisor


def test_no_settings_window_turns_an_llm_edit_into_a_restart() -> None:
    """The bug itself: any LLM key moving was promoted to a server restart.

    Asserted against the code that did it -- the LLM-key diff each window ran
    over its own pending updates -- rather than against prose describing it,
    which the comments explaining the removal would themselves match.
    """
    assert 'startswith(("LLM_"' not in WIN_SETTINGS
    assert "llm_keys = " not in MAC_SETTINGS


def test_only_the_port_can_ask_for_a_restart() -> None:
    for source in (MAC_SETTINGS, SETTINGS_HTML):
        assert "KIMO_WEB_PORT" in source
    assert "_ask_restart_for_port" in MAC_SETTINGS


def test_there_is_one_save_button_on_both_platforms() -> None:
    assert "onSaveRestart" not in MAC_SETTINGS
    assert 'id="save_restart"' not in SETTINGS_HTML
    assert 'id="save"' in SETTINGS_HTML
