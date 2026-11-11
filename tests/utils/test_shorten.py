"""Unit tests for the custom shorten() function in kimo_cli.utils.string."""

from __future__ import annotations


def test_short_text_returned_unchanged():
    from kimo_cli.utils.string import shorten

    assert shorten("hello", width=10) == "hello"


def test_exact_width_returned_unchanged():
    from kimo_cli.utils.string import shorten

    assert shorten("hello", width=5) == "hello"


def test_truncates_at_word_boundary():
    from kimo_cli.utils.string import shorten

    result = shorten("hello world foo bar", width=12)
    assert result == "hello world…"


def test_cjk_text_hard_cut_no_collapse():
    """CJK text without spaces must NOT collapse to just the placeholder."""
    from kimo_cli.utils.string import shorten

    text = "这是一段很长的中文文本用于测试截断功能"
    result = shorten(text, width=10)
    assert len(result) <= 10
    assert result == "这是一段很长的中文文本"[:9] + "…"
    # Key assertion: result is NOT just the placeholder
    assert result != "…"


def test_whitespace_normalised():
    from kimo_cli.utils.string import shorten

    result = shorten("hello   world\nfoo", width=20)
    assert result == "hello world foo"


def test_empty_string():
    from kimo_cli.utils.string import shorten

    assert shorten("", width=10) == ""


def test_width_equals_one_with_long_text():
    """Edge case: width=1 with text longer than 1 char."""
    from kimo_cli.utils.string import shorten

    result = shorten("hello", width=1)
    assert len(result) <= 1


def test_custom_placeholder():
    from kimo_cli.utils.string import shorten

    result = shorten("hello world foo bar", width=12, placeholder="...")
    assert result.endswith("...")
    assert len(result) <= 12


def test_placeholder_longer_than_cut():
    """When cut <= 0, fall back to hard cut without placeholder."""
    from kimo_cli.utils.string import shorten

    result = shorten("hello", width=1, placeholder="...")
    assert len(result) <= 1
    assert result == "h"


def test_shorten_middle_never_exceeds_the_width():
    """The result must fit into ``width`` — the old version returned width+3."""
    from kimo_cli.utils.string import shorten_middle

    text = "x" * 200
    for width in (1, 2, 3, 4, 10, 50, 51):
        result = shorten_middle(text, width=width)
        assert len(result) <= width, f"width={width} produced {len(result)} chars"


def test_shorten_middle_with_width_one_keeps_a_single_character():
    from kimo_cli.utils.string import shorten_middle

    assert shorten_middle("hello", width=1) == "h"


def test_shorten_middle_keeps_both_ends():
    from kimo_cli.utils.string import shorten_middle

    result = shorten_middle("abcdefghij", width=7)

    # The head keeps ``width // 2`` characters (25 for the 50-char budget the
    # tool-call display uses), the rest of the budget goes to the tail.
    assert len(result) == 7
    assert result.startswith("abc")
    assert result.endswith("j")
    assert "..." in result


def test_shorten_middle_keeps_the_head_length_callers_expect():
    from kimo_cli.utils.string import shorten_middle

    result = shorten_middle("sleep 2 && printf should-not-exist > cancel_output.txt", width=50)

    assert len(result) == 50
    assert result.startswith("sleep 2 && printf should-")
