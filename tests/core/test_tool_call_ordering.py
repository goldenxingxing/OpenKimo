"""Tool calls in one reply take effect in the order the model wrote them.

Before, every call started at once. Two edits to the same file each read the file,
waited (for the diff, and for approval), then wrote what they had read plus their own
change — so the second write threw away the first edit.

Read-only calls still run side by side; a call that may change something waits for
every call before it, and every call after it waits for it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from kaos.path import KaosPath
from kosong.message import ToolCall
from kosong.tooling import CallableTool2, ToolOk, ToolResult, ToolReturnValue
from pydantic import BaseModel

from kimo_cli.soul.agent import Runtime
from kimo_cli.soul.approval import ApprovalResult
from kimo_cli.soul.toolset import KimoToolset
from kimo_cli.tools.file.replace import StrReplaceFile


class _Params(BaseModel):
    pass


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []


def _tool(name: str, recorder: _Recorder, delay: float = 0.03) -> CallableTool2[_Params]:
    async def __call__(self: CallableTool2[_Params], params: _Params) -> ToolReturnValue:
        recorder.events.append(("start", name))
        await asyncio.sleep(delay)
        recorder.events.append(("end", name))
        return ToolOk(output=name)

    cls = type(
        f"_Fake_{name}",
        (CallableTool2,),
        {"name": name, "description": name, "params": _Params, "__call__": __call__},
    )
    return cls()


def _call(call_id: str, name: str, arguments: dict[str, object] | None = None) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolCall.FunctionBody(name=name, arguments=json.dumps(arguments or {})),
    )


async def _run(toolset: KimoToolset, calls: list[ToolCall]) -> list[ToolResult]:
    toolset.begin_step([])
    handles = [toolset.handle(call) for call in calls]
    results = [h if isinstance(h, ToolResult) else await h for h in handles]
    toolset.end_step()
    return results


def _overlap(events: list[tuple[str, str]], a: str, b: str) -> bool:
    """Whether a and b were running at the same time."""
    running: set[str] = set()
    for kind, name in events:
        if kind == "start":
            running.add(name)
            if {a, b} <= running:
                return True
        else:
            running.discard(name)
    return False


def _ended_before_started(events: list[tuple[str, str]], first: str, second: str) -> bool:
    return events.index(("end", first)) < events.index(("start", second))


async def test_read_only_calls_run_side_by_side() -> None:
    recorder = _Recorder()
    toolset = KimoToolset()
    for name in ("ReadFile", "Grep", "Glob"):
        toolset.add(_tool(name, recorder))

    await _run(toolset, [_call("1", "ReadFile"), _call("2", "Grep"), _call("3", "Glob")])

    assert _overlap(recorder.events, "ReadFile", "Grep")
    assert _overlap(recorder.events, "Grep", "Glob")


async def test_calls_that_may_change_things_run_one_after_another() -> None:
    recorder = _Recorder()
    toolset = KimoToolset()
    for name in ("WriteFile", "Shell"):
        toolset.add(_tool(name, recorder))

    await _run(toolset, [_call("1", "WriteFile"), _call("2", "Shell")])

    assert _ended_before_started(recorder.events, "WriteFile", "Shell")


async def test_a_read_after_a_write_sees_the_write_and_a_write_waits_for_earlier_reads() -> None:
    recorder = _Recorder()
    toolset = KimoToolset()
    for name in ("ReadFile", "WriteFile", "Grep"):
        toolset.add(_tool(name, recorder))

    await _run(toolset, [_call("1", "ReadFile"), _call("2", "WriteFile"), _call("3", "Grep")])

    assert _ended_before_started(recorder.events, "ReadFile", "WriteFile")
    assert _ended_before_started(recorder.events, "WriteFile", "Grep")


async def test_unknown_tools_are_treated_as_writes() -> None:
    recorder = _Recorder()
    toolset = KimoToolset()
    for name in ("mcp_thing", "ReadFile"):
        toolset.add(_tool(name, recorder))

    await _run(toolset, [_call("1", "mcp_thing"), _call("2", "ReadFile")])

    assert _ended_before_started(recorder.events, "mcp_thing", "ReadFile")


async def test_results_keep_the_order_of_the_calls() -> None:
    recorder = _Recorder()
    toolset = KimoToolset()
    toolset.add(_tool("WriteFile", recorder, delay=0.05))
    toolset.add(_tool("ReadFile", recorder, delay=0.0))

    results = await _run(toolset, [_call("a", "WriteFile"), _call("b", "ReadFile")])

    assert [r.tool_call_id for r in results] == ["a", "b"]


class _GatedApproval:
    """Holds every approval request until the test lets it through."""

    def __init__(self) -> None:
        self.pending: list[asyncio.Future[bool]] = []

    async def request(self, sender, action, description, display=None, *, request_policy="default"):  # noqa: ANN001, ANN201
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending.append(future)
        return ApprovalResult(approved=await future)


async def _wait_for(predicate, timeout: float = 5.0) -> None:  # noqa: ANN001
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def test_two_approved_edits_to_one_file_both_survive(
    runtime: Runtime, temp_work_dir: KaosPath
) -> None:
    target = Path(str(temp_work_dir)) / "notes.txt"
    target.write_text("alpha\nbeta\n")
    gated = _GatedApproval()
    toolset = KimoToolset()
    toolset.add(StrReplaceFile(runtime=runtime, approval=gated))  # type: ignore[arg-type]

    toolset.begin_step([])
    edit_alpha = toolset.handle(
        _call(
            "a", "StrReplaceFile", {"path": str(target), "edit": {"old": "alpha", "new": "ALPHA"}}
        )
    )
    edit_beta = toolset.handle(
        _call("b", "StrReplaceFile", {"path": str(target), "edit": {"old": "beta", "new": "BETA"}})
    )
    assert not isinstance(edit_alpha, ToolResult) and not isinstance(edit_beta, ToolResult)

    # Approve whichever edit is waiting, one at a time.
    await _wait_for(lambda: len(gated.pending) >= 1)
    gated.pending[0].set_result(True)
    await _wait_for(lambda: len(gated.pending) >= 2 or (edit_alpha.done() and edit_beta.done()))
    if len(gated.pending) >= 2:
        gated.pending[1].set_result(True)
    results = [await edit_alpha, await edit_beta]
    toolset.end_step()

    assert all(not r.return_value.is_error for r in results), results
    assert target.read_text() == "ALPHA\nBETA\n"


@pytest.mark.parametrize(
    ("read_only_hint", "alongside"), [(True, True), (False, False), (None, False)]
)
def test_an_mcp_tool_runs_alongside_only_when_its_server_marks_it_read_only(
    runtime: Runtime, read_only_hint: bool | None, alongside: bool
) -> None:
    import mcp

    from kimo_cli.soul.toolset import MCPTool, _runs_alongside

    annotations = (
        None if read_only_hint is None else mcp.types.ToolAnnotations(readOnlyHint=read_only_hint)
    )
    tool = MCPTool(
        "server",
        mcp.Tool(name="lookup", inputSchema={"type": "object"}, annotations=annotations),
        client=None,  # type: ignore[arg-type]  # never called here
        runtime=runtime,
    )
    assert _runs_alongside(tool) is alongside
