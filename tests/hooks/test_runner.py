import asyncio
import os
import shlex
import subprocess
import sys

import pytest

from kimo_cli.hooks.runner import run_hook


def _py(code: str) -> str:
    """A cross-platform ``python -c`` command for run_hook."""
    if os.name == "nt":
        return subprocess.list2cmdline([sys.executable, "-c", code])
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


@pytest.mark.asyncio
async def test_exit_0_allows():
    result = await run_hook("echo ok", {"tool_name": "Shell"}, timeout=5)
    assert result.action == "allow"
    assert result.stdout.strip() == "ok"


@pytest.mark.asyncio
async def test_exit_2_blocks():
    result = await run_hook(
        _py("import sys; sys.stderr.write('blocked'); sys.exit(2)"), {"tool_name": "Shell"}, timeout=5
    )
    assert result.action == "block"
    assert "blocked" in result.reason


@pytest.mark.asyncio
async def test_exit_1_allows():
    result = await run_hook("exit 1", {"tool_name": "Shell"}, timeout=5)
    assert result.action == "allow"


@pytest.mark.asyncio
async def test_timeout_allows():
    result = await run_hook("sleep 10", {"tool_name": "Shell"}, timeout=1)
    assert result.action == "allow"
    assert result.timed_out


@pytest.mark.asyncio
async def test_json_deny_decision():
    cmd = _py("import json; print(json.dumps({'hookSpecificOutput': {'permissionDecision': 'deny', 'permissionDecisionReason': 'use rg'}}))")
    result = await run_hook(cmd, {"tool_name": "Bash"}, timeout=5)
    assert result.action == "block"
    assert result.reason == "use rg"


@pytest.mark.asyncio
async def test_stdin_receives_json():
    cmd = _py("import sys,json; d=json.load(sys.stdin); print(d['tool_name'])")
    result = await run_hook(cmd, {"tool_name": "WriteFile"}, timeout=5)
    assert result.stdout.strip() == "WriteFile"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        '{"hookSpecificOutput": null}',
        '{"hookSpecificOutput": "oops"}',
        '{"hookSpecificOutput": []}',
        '{"hookSpecificOutput": 3}',
        '{"hookSpecificOutput": {"permissionDecision": null}}',
    ],
)
async def test_malformed_hook_specific_output_fails_open(payload: str):
    """`hookSpecificOutput` is third-party data; a bad shape must allow, not raise."""
    result = await run_hook(f"printf '%s' '{payload}'", {"tool_name": "Bash"}, timeout=5)
    assert result.action == "allow"


@pytest.mark.asyncio
async def test_deny_still_blocks_alongside_malformed_siblings():
    cmd = """printf '%s' '{"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "no rm"}}'"""
    result = await run_hook(cmd, {"tool_name": "Bash"}, timeout=5)
    assert result.action == "block"
    assert result.reason == "no rm"


@pytest.mark.asyncio
async def test_child_is_reaped_when_the_input_cannot_be_encoded(tmp_path):
    """A hook that fails before its input is written must not survive as an orphan."""
    marker = tmp_path / "still-running.txt"
    command = f"sleep 2; touch {marker}"

    # A non-serialisable payload makes ``json.dumps`` raise inside ``run_hook``.
    result = await run_hook(command, {"bad": object()}, timeout=10)

    assert result.action == "allow"
    await asyncio.sleep(2.5)
    assert not marker.exists(), "the hook process was left running"
