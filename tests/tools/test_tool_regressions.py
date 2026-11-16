"""Dedicated regression tests for the tool-layer findings.

Each test here reproduces the original defect: reverting the corresponding fix
in ``src/kimo_cli/tools/`` must make it fail.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from kimo_cli.tools.agent import MAX_AGENT_TIMEOUT
from kimo_cli.tools.agent import Params as AgentParams
from kimo_cli.tools.file.read import ReadFile
from kimo_cli.tools.file.utils import detect_file_type
from kimo_cli.tools.utils import ToolResultBuilder

# ---------------------------------------------------------------------------
# F069 / F071 — the agent timeout bound is described by one constant
# ---------------------------------------------------------------------------


def test_agent_timeout_bound_uses_the_merged_constant() -> None:
    """The field bound and the description must not drift apart."""
    AgentParams(description="d", prompt="p", timeout=MAX_AGENT_TIMEOUT)
    AgentParams(description="d", prompt="p", timeout=30)
    with pytest.raises(ValidationError):
        AgentParams(description="d", prompt="p", timeout=MAX_AGENT_TIMEOUT + 1)


def test_agent_no_longer_carries_two_identical_timeout_constants() -> None:
    """Two same-valued constants made the foreground/background split a lie."""
    import kimo_cli.tools.agent as agent_module

    assert not hasattr(agent_module, "MAX_FOREGROUND_TIMEOUT")
    assert not hasattr(agent_module, "MAX_BACKGROUND_TIMEOUT")
    assert MAX_AGENT_TIMEOUT == 3600


# ---------------------------------------------------------------------------
# F073 — glob tells the failing stage apart
# ---------------------------------------------------------------------------


async def test_glob_reports_the_stage_that_failed(
    glob_tool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search failure is logged as a search failure, not as a generic error."""
    from kimo_cli import logger as kimo_logger

    records: list[str] = []
    kimo_logger.enable("kimo_cli")
    sink_id = kimo_logger.add(lambda message: records.append(str(message)), level="WARNING")
    try:
        result = await glob_tool(_absolute_pattern_params())
    finally:
        kimo_logger.remove(sink_id)

    assert result.is_error
    assert any("Glob search failed" in record for record in records)
    assert not any("Glob argument validation failed" in record for record in records)


def _absolute_pattern_params():
    from kimo_cli.tools.file.glob import Params as GlobParams

    # pathlib refuses non-relative patterns; that is a search-stage failure.
    return GlobParams(pattern="/definitely/absolute/*")


# ---------------------------------------------------------------------------
# F077 — the ripgrep line regex is compiled once
# ---------------------------------------------------------------------------


def test_rg_line_regex_lives_at_module_level() -> None:
    """Compiling it per call was pointless work in the hot path."""
    import kimo_cli.tools.file.grep_local as grep_local

    assert grep_local._RG_LINE_RE.pattern == r"^(.*?)([:\-])(\d+)\2"
    source = inspect.getsource(grep_local.Grep.__call__)
    assert "re.compile" not in source


# ---------------------------------------------------------------------------
# F079 / F080 — ReadFile's tail path
# ---------------------------------------------------------------------------


async def test_read_tail_never_claims_the_line_cap_was_reached(
    read_file_tool, tmp_path: Path
) -> None:
    """``max_lines_reached`` was provably false, so the branch was dead."""
    from kimo_cli.tools.file.read import MAX_LINES
    from kimo_cli.tools.file.read import Params as ReadParams

    path = tmp_path / "long.txt"
    path.write_text("".join(f"line {i}\n" for i in range(MAX_LINES + 50)), encoding="utf-8")

    result = await read_file_tool(ReadParams(path=str(path), line_offset=-MAX_LINES))

    assert not result.is_error
    assert "Max 1000 lines reached" not in result.message
    assert "lines read from file" in result.message
    # The branch itself is gone, so it cannot come back unnoticed.
    assert "max_lines_reached" not in inspect.getsource(ReadFile._read_tail)


async def test_read_rejects_a_zero_line_count_without_assert(
    read_file_tool, tmp_path: Path
) -> None:
    """``assert`` is stripped under ``python -O``; the guard must survive it."""
    from kimo_cli.tools.file.read import Params as ReadParams

    path = tmp_path / "x.txt"
    path.write_text("hello\n", encoding="utf-8")
    params = ReadParams.model_construct(path=str(path), n_lines=0, line_offset=1)

    result = await read_file_tool(params)

    assert result.is_error
    assert "n_lines must be positive" in result.message


async def test_read_rejects_a_zero_line_offset_without_assert(
    read_file_tool, tmp_path: Path
) -> None:
    from kimo_cli.tools.file.read import Params as ReadParams

    path = tmp_path / "x.txt"
    path.write_text("hello\n", encoding="utf-8")
    params = ReadParams.model_construct(path=str(path), n_lines=10, line_offset=0)

    result = await read_file_tool(params)

    assert result.is_error
    assert "line_offset must not be zero" in result.message


# ---------------------------------------------------------------------------
# F081 / F083 — ReadMediaFile wording and staging
# ---------------------------------------------------------------------------


async def test_read_media_too_large_message_has_no_duplicated_word(
    read_media_file_tool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wording used to say "the max maximum size"."""
    import kimo_cli.tools.file.read_media as read_media

    monkeypatch.setattr(read_media, "MAX_MEDIA_MEGABYTES", 0)
    path = tmp_path / "big.png"
    path.write_bytes(_PNG_MAGIC + b"\x00" * 64)

    result = await read_media_file_tool(_read_media_params(path))

    assert result.is_error
    assert "max maximum" not in result.message
    assert "exceeds the maximum size of" in result.message


async def test_read_media_reports_the_failing_stage(
    read_media_file_tool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path probing and media reading fail differently and say so."""
    import kimo_cli.tools.file.read_media as read_media
    from kimo_cli import logger as kimo_logger

    def _boom(*_args, **_kwargs):
        raise RuntimeError("sniff failed")

    monkeypatch.setattr(read_media, "detect_file_type", _boom)
    path = tmp_path / "present.png"
    path.write_bytes(_PNG_MAGIC + b"\x00" * 64)

    records: list[str] = []
    kimo_logger.enable("kimo_cli")
    sink_id = kimo_logger.add(lambda message: records.append(str(message)), level="WARNING")
    try:
        result = await read_media_file_tool(_read_media_params(path))
    finally:
        kimo_logger.remove(sink_id)

    assert result.is_error
    assert any("ReadMediaFile path check failed" in record for record in records)
    assert not any("ReadMediaFile failed" in record for record in records)


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _read_media_params(path: Path):
    from kimo_cli.tools.file.read_media import Params as ReadMediaParams

    return ReadMediaParams(path=str(path))


# ---------------------------------------------------------------------------
# F085 — media sniffing versus a text hint
# ---------------------------------------------------------------------------


def test_detect_file_type_rejects_media_magic_inside_a_text_file() -> None:
    """A ``.svg`` file whose bytes are a PNG is not a usable image."""
    png_magic = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

    result = detect_file_type("logo.svg", header=png_magic)

    assert result.kind == "unknown"


def test_detect_file_type_accepts_a_plain_text_file() -> None:
    result = detect_file_type("notes.md", header=b"# hello")

    assert result.kind == "text"


def test_detect_file_type_no_longer_compares_a_always_different_kind() -> None:
    """At that point the hint can only be a text one, so the comparison is dead."""
    import kimo_cli.tools.file.utils as file_utils

    source = inspect.getsource(file_utils.detect_file_type)
    assert "sniffed.kind != media_hint.kind" not in source


# ---------------------------------------------------------------------------
# F086 — WriteFile keeps a defence-in-depth mode check
# ---------------------------------------------------------------------------


async def test_write_rejects_an_unknown_mode(write_file_tool, tmp_path: Path) -> None:
    """The Literal makes the branch unreachable through validation, not through code."""
    from kimo_cli.tools.file.write import Params as WriteParams

    path = tmp_path / "out.txt"
    params = WriteParams.model_construct(path=str(path), content="x", mode="bogus")

    result = await write_file_tool(params)

    assert result.is_error
    assert "Invalid write mode" in result.message
    assert not path.exists()


# ---------------------------------------------------------------------------
# F094 — the todo tool says when it could not persist
# ---------------------------------------------------------------------------


async def test_todo_warns_when_a_subagent_has_no_state_file(
    runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silently reporting "updated" for a write that never happened is a lie."""
    from kimo_cli import logger as kimo_logger
    from kimo_cli.tools.todo import SetTodoList

    monkeypatch.setattr(runtime, "role", "subagent", raising=False)
    monkeypatch.setattr(runtime, "subagent_store", None, raising=False)
    monkeypatch.setattr(runtime, "subagent_id", None, raising=False)

    records: list[str] = []
    kimo_logger.enable("kimo_cli")
    sink_id = kimo_logger.add(lambda message: records.append(str(message)), level="WARNING")
    try:
        tool = SetTodoList(runtime)
        tool._save_subagent_todos([])
    finally:
        kimo_logger.remove(sink_id)

    assert any("Subagent todos not persisted" in record for record in records)


# ---------------------------------------------------------------------------
# F097 — the builder rejects a budget that cannot hold the marker
# ---------------------------------------------------------------------------


def test_result_builder_rejects_a_line_budget_below_the_marker() -> None:
    """``assert`` disappears under ``python -O``; the check must not."""
    with pytest.raises(ValueError, match="max_line_length"):
        ToolResultBuilder(max_line_length=5)
