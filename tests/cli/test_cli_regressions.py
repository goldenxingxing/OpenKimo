"""Regression tests for CLI-side findings (export, mcp, plugin, entry phases)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from kimo_cli.cli import export as export_module
from kimo_cli.cli import mcp as mcp_module
from kimo_cli.cli.export import _is_safe_session_id


@pytest.mark.parametrize("session_id", ["..", ".", "", "../etc", "a/b", "a\\b"])
def test_session_id_must_be_a_single_segment(session_id: str) -> None:
    assert not _is_safe_session_id(session_id)


@pytest.mark.parametrize("session_id", ["0b1c2d3e-0000-4000-8000-000000000000", "abc", "a.b_c"])
def test_session_id_accepts_normal_ids(session_id: str) -> None:
    assert _is_safe_session_id(session_id)


def test_find_session_by_id_refuses_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``..`` used to resolve to the sessions root and export every session."""
    share = tmp_path / "share"
    (share / "sessions" / "hash").mkdir(parents=True)
    monkeypatch.setattr("kimo_cli.share.get_share_dir", lambda: share)

    assert export_module._find_session_by_id("..") is None
    assert export_module._find_session_by_id("../hash") is None


def test_plugin_zip_suffix_is_case_insensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kimo_cli.cli import plugin as plugin_module

    archive = tmp_path / "myplugin.ZIP"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("plugin.json", json.dumps({"name": "demo"}))

    directory, cleanup = plugin_module._resolve_source(str(archive))
    try:
        assert (directory / "plugin.json").exists()
    finally:
        if cleanup is not None:
            import shutil

            shutil.rmtree(cleanup, ignore_errors=True)


def test_mcp_test_tolerates_oauth_stdio_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hand-edited ``auth: oauth`` stdio entry has no ``url``; that must not crash."""
    calls: list[dict[str, object]] = []

    class _Result:
        ok = True
        tools: list[object] = []
        error = ""

    async def _probe(name: str, server: dict[str, object], *, timeout_s: object) -> _Result:
        calls.append({"name": name, "server": server, "timeout_s": timeout_s})
        return _Result()

    monkeypatch.setattr(
        mcp_module, "_get_mcp_server", lambda name: {"command": "npx", "auth": "oauth"}
    )
    monkeypatch.setattr(
        mcp_module, "_has_oauth_tokens", lambda _url: pytest.fail("must not touch tokens")
    )
    monkeypatch.setattr("kimo_cli.mcp_probe.probe_mcp_server", _probe)

    mcp_module.mcp_test("demo")

    assert calls and calls[0]["server"] == {"command": "npx", "auth": "oauth"}


def test_escaping_exception_keeps_the_crash_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    """``set_phase("shutdown")`` in a finally hid where the crash actually happened."""
    from kimo_cli.cli import __main__ as cli_main
    from kimo_cli.telemetry import crash

    phases: list[str] = []
    monkeypatch.setattr(crash, "install_crash_handlers", lambda: None)
    monkeypatch.setattr(crash, "set_phase", phases.append)
    monkeypatch.setattr("kimo_cli.utils.proxy.normalize_proxy_env", lambda: None)

    def _boom(**_kwargs: object) -> None:
        raise RuntimeError("boom")

    # ``cli`` is imported at module level by the entry point, so patch it there.
    monkeypatch.setattr(cli_main, "cli", _boom)

    with pytest.raises(RuntimeError, match="boom"):
        cli_main.main([])

    assert "shutdown" not in phases


def test_normal_return_marks_the_shutdown_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    from kimo_cli.cli import __main__ as cli_main
    from kimo_cli.telemetry import crash

    phases: list[str] = []
    monkeypatch.setattr(crash, "install_crash_handlers", lambda: None)
    monkeypatch.setattr(crash, "set_phase", phases.append)
    monkeypatch.setattr("kimo_cli.utils.proxy.normalize_proxy_env", lambda: None)
    monkeypatch.setattr(cli_main, "cli", lambda **_kwargs: 0)

    assert cli_main.main([]) == 0
    assert phases == ["shutdown"]


def test_verbose_flag_changes_the_log_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--verbose`` was declared and then never read, so it did nothing."""
    from loguru import logger

    from kimo_cli.app import enable_logging

    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))
    try:
        enable_logging(verbose=True, redirect_stderr=False)
        verbose_levels = {handler.levelno for handler in logger._core.handlers.values()}
        enable_logging(redirect_stderr=False)
        default_levels = {handler.levelno for handler in logger._core.handlers.values()}
    finally:
        enable_logging(redirect_stderr=False)

    assert logger.level("DEBUG").no in verbose_levels
    assert logger.level("DEBUG").no not in default_levels
    assert logger.level("INFO").no in default_levels


def test_verbose_flag_is_passed_through_by_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag has to reach ``enable_logging``; before this it was unread."""
    seen: list[dict[str, object]] = []

    class _Stop(Exception):
        """Abort right after the flag is consumed, before the shell starts."""

    def _enable(debug: bool = False, **kwargs: object) -> None:
        seen.append({"debug": debug, **kwargs})
        raise _Stop

    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))
    monkeypatch.setattr("kimo_cli.app.enable_logging", _enable)

    from typer.testing import CliRunner

    from kimo_cli.cli import cli

    result = CliRunner().invoke(cli, ["--verbose", "--work-dir", str(tmp_path)])

    assert isinstance(result.exception, _Stop)
    assert seen and seen[0].get("verbose") is True


def test_plugin_zip_with_an_underscore_directory_is_still_found(tmp_path: Path) -> None:
    """The first scan already covers every child directory, underscore or not."""
    from kimo_cli.cli import plugin as plugin_module

    archive = tmp_path / "plugin.ZIP"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("_internal/plugin.json", json.dumps({"name": "demo"}))

    directory, cleanup = plugin_module._resolve_source(str(archive))
    try:
        assert (directory / "plugin.json").exists()
    finally:
        if cleanup is not None:
            import shutil

            shutil.rmtree(cleanup, ignore_errors=True)


def test_plugin_extraction_has_no_second_directory_scan() -> None:
    """The follow-up scan could never fire: the first one sees every child."""
    import inspect

    from kimo_cli.cli import plugin as plugin_module

    source = inspect.getsource(plugin_module._extract_zip_to_plugin)
    assert "dirs = [d for d in tmp.iterdir()" not in source


def test_mcp_remove_reports_a_missing_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """``del config["mcpServers"][name]`` raised a bare KeyError."""
    import typer

    from kimo_cli.cli import mcp as mcp_module

    monkeypatch.setattr(mcp_module, "_load_mcp_config", lambda: {"mcpServers": {}})

    with pytest.raises(typer.Exit) as excinfo:
        mcp_module.mcp_remove("ghost")

    assert excinfo.value.exit_code == 1
