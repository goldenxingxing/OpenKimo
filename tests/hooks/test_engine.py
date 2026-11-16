import asyncio
import time
from unittest.mock import patch

import pytest

from kimo_cli.hooks import engine as engine_module
from kimo_cli.hooks.config import HookDef
from kimo_cli.hooks.engine import HookEngine, WireHookSubscription
from kimo_cli.hooks.runner import HookResult


@pytest.fixture
def engine():
    hooks = [
        HookDef(event="PreToolUse", matcher="Shell|WriteFile", command="exit 0", timeout=5),
        HookDef(event="PreToolUse", matcher="ReadFile", command="exit 2", timeout=5),
        HookDef(event="Stop", matcher="", command="echo done", timeout=5),
    ]
    return HookEngine(hooks)


@pytest.mark.asyncio
async def test_match_tool_name(engine):
    results = await engine.trigger(
        "PreToolUse", matcher_value="Shell", input_data={"tool_name": "Shell"}
    )
    assert len(results) == 1
    assert results[0].action == "allow"


@pytest.mark.asyncio
async def test_no_match(engine):
    results = await engine.trigger("PreToolUse", matcher_value="Grep", input_data={})
    assert len(results) == 0


@pytest.mark.asyncio
async def test_block(engine):
    results = await engine.trigger("PreToolUse", matcher_value="ReadFile", input_data={})
    assert len(results) == 1
    assert results[0].action == "block"


@pytest.mark.asyncio
async def test_empty_matcher_matches_all(engine):
    results = await engine.trigger("Stop", matcher_value="anything", input_data={})
    assert len(results) == 1


@pytest.mark.asyncio
async def test_no_hooks_for_event(engine):
    results = await engine.trigger("UserPromptSubmit", matcher_value="", input_data={})
    assert len(results) == 0


@pytest.mark.asyncio
async def test_dedup_identical_commands():
    hooks = [
        HookDef(event="Stop", command="echo once", timeout=5),
        HookDef(event="Stop", command="echo once", timeout=5),
    ]
    engine = HookEngine(hooks)
    results = await engine.trigger("Stop", input_data={})
    assert len(results) == 1


@pytest.mark.asyncio
async def test_invalid_regex_skips_hook():
    hooks = [
        HookDef(event="PreToolUse", matcher="[invalid", command="exit 0", timeout=5),
    ]
    engine = HookEngine(hooks)
    # Should not raise, just skip the hook with invalid regex
    results = await engine.trigger("PreToolUse", matcher_value="Shell", input_data={})
    assert len(results) == 0


@pytest.mark.asyncio
async def test_telemetry_failure_does_not_discard_block_result(engine):
    """Safety-critical: a telemetry failure MUST NOT cause the hook engine
    to fail open. For PreToolUse block, dropping results to [] silently
    bypasses the block — that's exactly what this guard prevents.
    """
    with patch("kimo_cli.telemetry.track", side_effect=RuntimeError("telemetry broken")):
        results = await engine.trigger("PreToolUse", matcher_value="ReadFile", input_data={})
    assert len(results) == 1
    assert results[0].action == "block"


@pytest.mark.asyncio
async def test_failing_hook_does_not_discard_sibling_block(monkeypatch):
    """Safety-critical: one exploding hook must not erase a sibling's block.

    A bare `asyncio.gather` re-raises the first exception, `trigger` swallows it
    as fail-open and returns [], so a policy hook's deny would be silently lost.
    """

    async def fake_run_hook(command, input_data, *, timeout=30, cwd=None):
        if command == "boom":
            raise RuntimeError("hook exploded")
        return HookResult(action="block", reason="denied by policy")

    monkeypatch.setattr(engine_module, "run_hook", fake_run_hook)
    eng = HookEngine(
        [
            HookDef(event="PreToolUse", matcher="Shell", command="boom", timeout=5),
            HookDef(event="PreToolUse", matcher="Shell", command="deny", timeout=5),
        ]
    )

    results = await eng.trigger("PreToolUse", matcher_value="Shell", input_data={})

    assert [r.action for r in results] == ["block"]
    assert results[0].reason == "denied by policy"


@pytest.mark.asyncio
async def test_wire_hook_callback_failure_resolves_without_waiting():
    """A dead wire callback must fail open at once, not stall until the timeout."""

    async def boom(handle):
        raise RuntimeError("client gone")

    eng = HookEngine(on_wire_hook=boom)
    eng.add_wire_subscriptions(
        [WireHookSubscription(id="w1", event="PreToolUse", matcher="Shell", timeout=30)]
    )

    t0 = time.monotonic()
    results = await eng.trigger("PreToolUse", matcher_value="Shell", input_data={})
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0, f"waited {elapsed:.1f}s for a callback that already died"
    assert [r.action for r in results] == ["allow"]
    assert not results[0].timed_out


def test_wire_hook_handle_uses_the_running_loop() -> None:
    """``get_event_loop()`` raises on 3.14 when no loop is current."""
    import inspect

    from kimo_cli.hooks.engine import WireHookHandle

    source = inspect.getsource(WireHookHandle._get_future)
    assert "asyncio.get_running_loop()" in source
    assert "asyncio.get_event_loop()" not in source


@pytest.mark.asyncio
async def test_wire_hook_handle_resolves_inside_a_loop() -> None:
    from kimo_cli.hooks.engine import WireHookHandle

    handle = WireHookHandle()

    handle.resolve("allow", "because")

    result = await asyncio.wait_for(handle.wait(), timeout=1)
    assert result.action == "allow"
