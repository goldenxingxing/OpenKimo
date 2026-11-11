"""Windows tray-app fixes: settings/mac parity, windowless spawn, uninstall task check."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_HTML = (ROOT / "packaging" / "app_main_win" / "settings.html").read_text(encoding="utf-8")
WIN_MAIN = (ROOT / "packaging" / "app_main_win" / "__main__.py").read_text(encoding="utf-8")
INSTALLER = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
MAC_SETTINGS = (ROOT / "packaging" / "app_main" / "settings_window.py").read_text(encoding="utf-8")


def _mac_tuple(name: str) -> list[str]:
    """Extract a tuple-of-strings constant from the macOS settings module.

    The non-empty assertion is the point. Every caller feeds this into a `for`
    loop, so an empty result runs no assertions and the parity test passes
    while checking nothing — and the parser is easy to defeat by accident: it
    assumes a parenthesised tuple of double-quoted strings, so switching the
    constant to a list, to single quotes, or adding a nested parenthesis
    silently yields []. Failing loudly here is what keeps the parity tests
    honest.
    """
    match = re.search(rf"{name}[^=]*=\s*\((.*?)\)", MAC_SETTINGS, re.S)
    assert match, f"{name} not found in macOS settings_window.py"
    values = re.findall(r'"([^"]+)"', match.group(1))
    assert values, (
        f"{name} matched but yielded no strings — the parser no longer "
        f"understands how it is declared, so every parity assertion over it "
        f"would pass vacuously"
    )
    return values


def test_provider_types_match_macos():
    for provider_type in _mac_tuple("_PROVIDER_TYPES"):
        assert f'"{provider_type}"' in SETTINGS_HTML


def test_capabilities_match_macos():
    for capability in _mac_tuple("_CAPABILITIES"):
        assert f'"{capability}"' in SETTINGS_HTML


def test_legacy_keys_cleared_on_save():
    for key in _mac_tuple("_LEGACY_LLM_KEYS"):
        assert key in SETTINGS_HTML


def test_settings_covers_macos_sections():
    # LLM: multi-provider editor + thinking + temperature
    for marker in (
        "LLM_PROVIDERS",
        "LLM_DEFAULT_PROVIDER",
        "LLM_THINKING",
        "LLM_TEMPERATURE",
        "add_provider",
        "Set as default",
        "Advanced (capabilities)",
        "max_context_size",
    ):
        assert marker in SETTINGS_HTML, f"missing LLM marker: {marker}"
    # Web Server: port + session token (+ generate) + LAN only
    for marker in (
        "KIMO_WEB_PORT",
        "KIMO_WEB_SESSION_TOKEN",
        "KIMO_WEB_LAN_ONLY",
        "generate_token",
    ):
        assert marker in SETTINGS_HTML, f"missing web marker: {marker}"
    # Branding reset
    assert "reset_branding" in SETTINGS_HTML


def test_settings_api_has_reset_branding():
    win_settings = (ROOT / "packaging" / "app_main_win" / "settings_window.py").read_text(
        encoding="utf-8"
    )
    assert "def reset_branding" in win_settings


def test_settings_subprocess_is_windowless():
    assert "pythonw.exe" in WIN_MAIN
    assert "CREATE_NO_WINDOW" in WIN_MAIN


def test_uninstall_does_not_call_wizard_functions():
    # WizardIsTaskSelected raises at uninstall time; the choice must be
    # persisted at install time and read back from the marker file.
    code_section = INSTALLER[INSTALLER.index("[Code]") :]
    uninstall_proc = code_section[code_section.index("CurUninstallStepChanged") :]
    assert "WizardIsTaskSelected" not in uninstall_proc
    assert "purge-user-data.marker" in INSTALLER
    assert "CurStepChanged" in INSTALLER


def test_installer_closes_a_running_openkimo_itself():
    # The Restart Manager listed a running OpenKimo as three "Python"s and then
    # failed to close them (no window to ask). Setup finds what runs from the
    # install dir and ends it, before installing and before uninstalling.
    setup = INSTALLER[INSTALLER.index("[Setup]") : INSTALLER.index("[Languages]")]
    code = INSTALLER[INSTALLER.index("[Code]") :]
    assert "CloseApplications=no" in setup
    assert "Win32_Process" in code and "taskkill.exe" in code
    for event in ("function PrepareToInstall(", "function InitializeUninstall("):
        body = code[code.index(event) :]
        body = body[: body.index("\nend;")]
        assert "CloseRunningApp()" in body
