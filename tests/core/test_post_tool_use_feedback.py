"""A PostToolUse hook can tell the model what went wrong with what it just did.

Before, PostToolUse hooks ran in the background and whatever they said was dropped,
so a linter run after each edit could not reach the model. Now the hook is awaited,
and its feedback is added to the result of the call it ran after — the way aider
feeds lint and test failures back — for at most MAX_HOOK_FEEDBACK_ROUNDS steps a turn.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

from kosong.message import ToolCall
from kosong.tooling import CallableTool2, ToolOk, ToolResult, ToolReturnValue
from pydantic import BaseModel

from kimo_cli.hooks.config import HookDef
from kimo_cli.hooks.engine import HookEngine
from kimo_cli.hooks.runner import run_hook
from kimo_cli.soul.toolset import MAX_HOOK_FEEDBACK_ROUNDS, KimoToolset


class _Params(BaseModel):
    content: str = ""


class _WriteFile(CallableTool2[_Params]):
    name: str = "WriteFile"
    description: str = "write"
    params: type[_Params] = _Params

    async def __call__(self, params: _Params) -> ToolReturnValue:
        return ToolOk(output="written")


def _py(code: str) -> str:
    if os.name == "nt":
        # cmd.exe does not understand POSIX single quotes from shlex.quote.
        return subprocess.list2cmdline([sys.executable, "-c", code])
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _toolset(command: str, matcher: str = "WriteFile") -> KimoToolset:
    toolset = KimoToolset()
    toolset.add(_WriteFile())
    toolset.set_hook_engine(
        HookEngine([HookDef(event="PostToolUse", command=command, matcher=matcher)])
    )
    return toolset


async def _write(toolset: KimoToolset, step_no: int = 1, call_id: str = "c1") -> ToolResult:
    toolset.begin_step([], step_no=step_no)
    handle = toolset.handle(
        ToolCall(
            id=call_id,
            function=ToolCall.FunctionBody(
                name="WriteFile", arguments=json.dumps({"content": call_id})
            ),
        )
    )
    result = handle if isinstance(handle, ToolResult) else await handle
    toolset.end_step()
    return result


def _output(result: ToolResult) -> str:
    output = result.return_value.output
    return output if isinstance(output, str) else "".join(getattr(p, "text", "") for p in output)


LINT_FAILS = _py("import sys; sys.stderr.write('app.py:3: E501 line too long'); sys.exit(2)")


async def test_exit_code_2_feeds_stderr_back_to_the_model() -> None:
    result = await _write(_toolset(LINT_FAILS))

    assert "written" in _output(result)
    assert "app.py:3: E501 line too long" in _output(result)


async def test_decision_block_feeds_the_reason_back() -> None:
    command = _py(
        "import json; print(json.dumps({'decision': 'block', 'reason': '2 tests failed'}))"
    )
    result = await _write(_toolset(command))

    assert "2 tests failed" in _output(result)


async def test_additional_context_is_passed_on() -> None:
    command = _py(
        "import json; print(json.dumps({'hookSpecificOutput': "
        "{'hookEventName': 'PostToolUse', 'additionalContext': 'type check: ok'}}))"
    )
    result = await _write(_toolset(command))

    assert "type check: ok" in _output(result)


async def test_a_quiet_hook_leaves_the_result_alone() -> None:
    result = await _write(_toolset(_py("pass")))

    assert _output(result) == "written"


async def test_a_hook_for_another_tool_does_not_run() -> None:
    result = await _write(_toolset(LINT_FAILS, matcher="Shell"))

    assert _output(result) == "written"


async def test_feedback_stops_after_the_cap_and_comes_back_next_turn() -> None:
    toolset = _toolset(LINT_FAILS)

    outputs = [
        _output(await _write(toolset, step_no=n, call_id=f"c{n}"))
        for n in range(1, MAX_HOOK_FEEDBACK_ROUNDS + 2)
    ]

    for output in outputs[:MAX_HOOK_FEEDBACK_ROUNDS]:
        assert "E501" in output
    assert "E501" not in outputs[MAX_HOOK_FEEDBACK_ROUNDS]
    assert "suppressed" in outputs[MAX_HOOK_FEEDBACK_ROUNDS]

    # a new turn starts again at step 1
    assert "E501" in _output(await _write(toolset, step_no=1, call_id="next-turn"))


async def test_runner_reads_decision_block_and_additional_context() -> None:
    blocked = await run_hook(
        _py("import json; print(json.dumps({'decision': 'block', 'reason': 'no'}))"), {}
    )
    assert (blocked.action, blocked.reason) == ("block", "no")

    context = await run_hook(
        _py("import json; print(json.dumps({'hookSpecificOutput': {'additionalContext': 'fyi'}}))"),
        {},
    )
    assert (context.action, context.additional_context) == ("allow", "fyi")
