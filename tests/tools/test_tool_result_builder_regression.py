"""Regression tests for tool result truncation."""

from kimo_cli.tools.utils import ToolResultBuilder, truncate_line


def test_truncation_never_exceeds_character_budget() -> None:
    builder = ToolResultBuilder(max_chars=10)

    builder.write("Hello")
    builder.write(" world!")
    result = builder.ok()

    assert isinstance(result.output, str)
    assert len(result.output) <= 10
    assert builder.n_chars <= 10
    assert "Output is truncated" in result.message


def test_write_after_exact_fill_marks_output_as_truncated() -> None:
    builder = ToolResultBuilder(max_chars=5)

    assert builder.write("Hello") == 5
    assert builder.write(" world") == 0
    result = builder.ok()

    assert result.output == "Hello"
    assert "Output is truncated" in result.message


def test_truncate_line_respects_budget_smaller_than_marker() -> None:
    assert truncate_line("abcdef\n", 2, marker="[...truncated]") == "ab"


def test_truncate_line_reports_the_exact_kept_prefix() -> None:
    """The offload path needs the cut position, not a guess from the layout.

    The original line already ends with the marker text, which the old
    length-arithmetic approach mis-read as "nothing was cut".
    """
    from kimo_cli.tools.utils import _truncate_line

    marker = "[...truncated]"
    line = "y" * 50 + marker + "\n"
    truncated, kept = _truncate_line(line, 40, marker)

    assert kept == 40 - len(marker) - 1
    assert truncated == line[:kept] + marker + "\n"
    assert line[kept:] == "y" * (50 - kept) + marker + "\n"
    assert len(truncated) == 40


def test_truncate_line_reports_full_length_when_nothing_is_cut() -> None:
    from kimo_cli.tools.utils import _truncate_line

    line = "short\n"
    truncated, kept = _truncate_line(line, 100)

    assert truncated == line
    assert kept == len(line)
