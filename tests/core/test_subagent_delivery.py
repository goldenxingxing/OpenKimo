"""A subagent reports in a fixed shape the parent can rely on.

The only requirement used to be length: a final reply under 200 characters was sent
back as "too brief", so a short, complete answer was padded, and a long one could
leave out what the parent needed. Now every task asks for a <result>, the <evidence>
behind it, and <open_questions> — decisions for the user go there instead of being
settled by the subagent, since the parent is the one who can ask. A complete report is
accepted however short; a short one without that shape is asked for once more.
"""

from __future__ import annotations

from kimo_cli.subagents.runner import (
    DELIVERY_INSTRUCTIONS,
    SUMMARY_CONTINUATION_PROMPT,
    missing_delivery_sections,
    needs_more_summary,
    with_delivery_instructions,
)

COMPLETE = (
    "<result>Fixed the off-by-one in parser.py.</result>\n"
    "<evidence>tests/test_parser.py passes (12 passed).</evidence>\n"
    "<open_questions>none</open_questions>"
)


def test_every_task_asks_for_the_report_shape() -> None:
    prompt = with_delivery_instructions("Find where the config is loaded.")

    assert prompt.startswith("Find where the config is loaded.")
    assert DELIVERY_INSTRUCTIONS in prompt
    for tag in ("<result>", "<evidence>", "<open_questions>"):
        assert tag in DELIVERY_INSTRUCTIONS
    assert "decide" in DELIVERY_INSTRUCTIONS.lower()  # user decisions are not settled here


def test_a_complete_report_is_accepted_however_short() -> None:
    assert len(COMPLETE) < 200
    assert missing_delivery_sections(COMPLETE) == []
    assert not needs_more_summary(COMPLETE)


def test_a_report_with_an_empty_result_is_not_complete() -> None:
    report = "<result> </result><evidence>x</evidence><open_questions>none</open_questions>"

    assert missing_delivery_sections(report) == ["result"]


def test_a_short_reply_without_the_shape_is_asked_for_again() -> None:
    assert needs_more_summary("Done.")
    for tag in ("<result>", "<evidence>", "<open_questions>"):
        assert tag in SUMMARY_CONTINUATION_PROMPT


def test_a_long_reply_without_the_shape_is_passed_on_as_before() -> None:
    assert not needs_more_summary("x" * 250)
