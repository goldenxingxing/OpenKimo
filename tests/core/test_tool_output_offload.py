"""Oversized tool output is offloaded to disk instead of being lost to truncation.

Covered paths: the standalone offloader, the streaming sink used by
``ToolResultBuilder``, and the MCP result conversion (Playwright-style
100k+ DOM dumps are the motivating case).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.client.client import CallToolResult
from kosong.tooling import CallableTool2, ToolReturnValue
from mcp.types import TextContent
from pydantic import BaseModel

from kimo_cli.soul.agent import Runtime
from kimo_cli.soul.offload import ToolOutputOffloader
from kimo_cli.soul.toolset import MCP_MAX_OUTPUT_CHARS, convert_mcp_tool_result
from kimo_cli.tools.utils import ToolResultBuilder


def test_offload_text_writes_full_content(tmp_path: Path) -> None:
    offloader = ToolOutputOffloader(tmp_path / "offload")
    text = "line\n" * 1000
    path = offloader.offload_text(text, tool_name="Shell")
    assert path.parent == tmp_path / "offload"
    assert "Shell" in path.name
    assert path.read_text(encoding="utf-8") == text


def test_sink_streams_and_close_keeps_file(tmp_path: Path) -> None:
    offloader = ToolOutputOffloader(tmp_path / "offload")
    sink = offloader.new_sink(tool_name="Grep")
    sink.write("first\n")
    sink.write("second\n")
    path = sink.close()
    assert path is not None
    assert path.read_text(encoding="utf-8") == "first\nsecond\n"


def test_sink_close_without_writes_leaves_no_file(tmp_path: Path) -> None:
    offloader = ToolOutputOffloader(tmp_path / "offload")
    sink = offloader.new_sink(tool_name="Grep")
    assert sink.close() is None
    assert not sink.path.exists()


def test_builder_streams_overflow_to_disk(tmp_path: Path) -> None:
    """Output past max_chars lands in the offload file; the file holds the
    buffered part plus the overflow — the full output, not just the tail."""
    offloader = ToolOutputOffloader(tmp_path / "offload")
    builder = ToolResultBuilder(
        max_chars=100, max_line_length=None, offloader=offloader, tool_name="Shell"
    )
    head = "h" * 90 + "\n"
    tail = "t" * 500 + "\n"
    builder.write(head)
    builder.write(tail)
    result = builder.ok()

    assert builder.is_full
    files = list((tmp_path / "offload").glob("*.txt"))
    assert len(files) == 1
    full = files[0].read_text(encoding="utf-8")
    assert full.startswith(head)
    assert full.endswith(tail)
    # The message must tell the model where the rest went.
    assert str(files[0]) in result.message
    assert "Read the file" in result.message


def test_builder_without_offloader_unchanged(tmp_path: Path) -> None:
    """Regression: no offloader means the historical plain truncation."""
    builder = ToolResultBuilder(max_chars=100, max_line_length=None)
    builder.write("h" * 90 + "\n")
    builder.write("t" * 500 + "\n")
    result = builder.ok()
    assert "truncated" in result.message
    assert "Full output saved" not in result.message
    assert not (tmp_path / "offload").exists()


def test_builder_error_also_carries_reference(tmp_path: Path) -> None:
    offloader = ToolOutputOffloader(tmp_path / "offload")
    builder = ToolResultBuilder(
        max_chars=100, max_line_length=None, offloader=offloader, tool_name="Shell"
    )
    builder.write("h" * 90 + "\n")
    builder.write("t" * 500 + "\n")
    result = builder.error("boom", brief="boom")
    assert result.is_error
    assert "Full output saved to:" in result.message


def test_mcp_oversized_text_is_offloaded(tmp_path: Path) -> None:
    offloader = ToolOutputOffloader(tmp_path / "offload")
    big = "d" * (MCP_MAX_OUTPUT_CHARS + 10_000)
    result = CallToolResult(
        content=[TextContent(type="text", text=big)], structured_content=None, meta=None
    )

    value = convert_mcp_tool_result(result, offloader=offloader, tool_name="playwright")

    files = list((tmp_path / "offload").glob("*playwright*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == big
    # The in-context part is truncated and carries the reference.
    texts = [p.text for p in value.output if hasattr(p, "text")]
    joined = "".join(texts)
    assert len(joined) < len(big)
    assert str(files[0]) in joined


def test_mcp_exhausted_budget_does_not_offload_every_remaining_part(tmp_path: Path) -> None:
    offloader = ToolOutputOffloader(tmp_path / "offload")
    big = "d" * (MCP_MAX_OUTPUT_CHARS + 1)
    result = CallToolResult(
        content=[
            TextContent(type="text", text=big),
            TextContent(type="text", text="x" * 1_000),
            TextContent(type="text", text="y" * 1_000),
        ],
        structured_content=None,
        meta=None,
    )

    value = convert_mcp_tool_result(result, offloader=offloader, tool_name="playwright")

    files = list((tmp_path / "offload").glob("*playwright*.txt"))
    assert len(files) == 1
    texts = [p.text for p in value.output if hasattr(p, "text")]
    assert len(texts) == 2  # budgeted first part + one truncation notice
    assert len(texts[0]) == MCP_MAX_OUTPUT_CHARS
    assert str(files[0]) in texts[0]
    assert "x" * 1_000 not in texts
    assert "y" * 1_000 not in texts

    big = "d" * (MCP_MAX_OUTPUT_CHARS + 10_000)
    result = CallToolResult(
        content=[TextContent(type="text", text=big)], structured_content=None, meta=None
    )
    value = convert_mcp_tool_result(result)
    texts = [p.text for p in value.output if hasattr(p, "text")]
    joined = "".join(texts)
    assert len(joined) < len(big)
    assert "Full output saved" not in joined


def test_mcp_small_result_untouched(tmp_path: Path) -> None:
    offloader = ToolOutputOffloader(tmp_path / "offload")
    result = CallToolResult(
        content=[TextContent(type="text", text="small")], structured_content=None, meta=None
    )
    value = convert_mcp_tool_result(result, offloader=offloader, tool_name="x")
    texts = [p.text for p in value.output if hasattr(p, "text")]
    assert texts == ["small"]
    assert not (tmp_path / "offload").exists()


class _Params(BaseModel):
    value: str = ""


class _BigOutputTool(CallableTool2[_Params]):
    """Builtin-style tool whose builder output exceeds the buffer."""

    name = "BigOutputTool"
    description = "Returns a large output via ToolResultBuilder."
    params = _Params

    async def __call__(self, params: _Params) -> ToolReturnValue:
        builder = ToolResultBuilder(max_chars=100, max_line_length=None, tool_name=self.name)
        builder.write("h" * 90 + "\n")
        builder.write("t" * 500 + "\n")
        return builder.ok()


@pytest.mark.asyncio
async def test_builtin_tool_offloads_via_handle_contextvar(
    runtime: Runtime, tmp_path: Path
) -> None:
    """No constructor change needed: the toolset binds the offloader to the
    in-flight call, and the builder picks it up from the contextvar."""
    from kosong.message import ToolCall

    from kimo_cli.soul.toolset import KimoToolset

    toolset = KimoToolset(runtime)
    toolset.add(_BigOutputTool())
    result_or_task = toolset.handle(
        ToolCall(
            id="call-1",
            function=ToolCall.FunctionBody(name="BigOutputTool", arguments="{}"),
        )
    )
    tool_result = await result_or_task
    value = tool_result.return_value

    offload_dir = runtime.session.dir / "offload"
    files = list(offload_dir.glob("*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8").endswith("t" * 500 + "\n")
    assert str(files[0]) in value.message
