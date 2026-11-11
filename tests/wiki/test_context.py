from __future__ import annotations

import pytest

from kimo_cli.wiki.context import (
    WIKI_BLOCK_END,
    WIKI_BLOCK_START,
    WIKI_PROMPT_MAX_BYTES,
    build_wiki_context,
    refresh_wiki_prompt_block,
    render_compact_index,
)

CHINESE_INDEX = """\
# Wiki Index

## Concepts
- [[concepts/普通]] — 普通知识
- [[concepts/并发]] — 并发写入
- [[concepts/恢复]] — 故障恢复
"""


def test_compact_index_preserves_utf8_entries_and_marker() -> None:
    rendered = render_compact_index(CHINESE_INDEX, max_bytes=120, max_entries=2)

    assert len(rendered.encode("utf-8")) <= 120
    assert rendered.endswith("<!-- Wiki index truncated -->")
    rendered.encode("utf-8").decode("utf-8")
    assert rendered.count("- [[") == 2


def test_compact_index_prioritizes_hinted_whole_entries() -> None:
    rendered = render_compact_index(
        CHINESE_INDEX,
        max_bytes=96,
        max_entries=1,
        hints=("并发",),
    )

    assert "[[concepts/并发]]" in rendered
    assert "[[concepts/普通]]" not in rendered
    assert "- [[concepts/并发]] — 并发写入" in rendered.splitlines()


def test_compact_index_returns_small_index_without_reformatting() -> None:
    rendered = render_compact_index(CHINESE_INDEX, max_bytes=512, max_entries=10)

    assert rendered == CHINESE_INDEX.strip()
    assert "truncated" not in rendered


@pytest.mark.parametrize(
    ("max_bytes", "max_entries"),
    [
        (0, 1),
        (20, 1),
        (120, -1),
        (True, 1),
        (120, True),
    ],
)
def test_compact_index_rejects_invalid_limits(max_bytes: int, max_entries: int) -> None:
    with pytest.raises(ValueError):
        render_compact_index(CHINESE_INDEX, max_bytes=max_bytes, max_entries=max_entries)


def test_refresh_inserts_wiki_block_into_pre_upgrade_prompt() -> None:
    old_prompt = "System prefix.\n\n# Skills\n\nKeep this user configuration."
    context = build_wiki_context(CHINESE_INDEX)

    refreshed = refresh_wiki_prompt_block(old_prompt, context)

    assert refreshed.startswith("System prefix.")
    assert refreshed.endswith("Keep this user configuration.")
    assert refreshed.count(WIKI_BLOCK_START) == 1
    assert refreshed.count(WIKI_BLOCK_END) == 1
    assert context in refreshed
    prefix, managed_and_suffix = refreshed.split(WIKI_BLOCK_START, 1)
    _managed, suffix = managed_and_suffix.split(WIKI_BLOCK_END, 1)
    assert prefix == "System prefix.\n"
    assert suffix == "\n# Skills\n\nKeep this user configuration."


def test_refresh_replaces_old_block_without_changing_other_prompt_content() -> None:
    old = f"Before.\n\n{WIKI_BLOCK_START}\n# Global Wiki\nstale index\n{WIKI_BLOCK_END}\n\nAfter."
    current = build_wiki_context(CHINESE_INDEX)

    refreshed = refresh_wiki_prompt_block(old, current)

    assert refreshed == (
        f"Before.\n\n{WIKI_BLOCK_START}\n# Knowledge Base\n{current}\n{WIKI_BLOCK_END}\n\nAfter."
    )
    assert "stale index" not in refreshed


def test_refresh_migrates_previous_unmarked_task9_block() -> None:
    legacy = (
        "# Global Wiki\n\n"
        "The global Wiki is shared across all workspaces.\n"
        "Use Wiki search/read for durable knowledge.\n"
        "Propose only durable, sourced conclusions for writing.\n\n"
        "# Wiki Index\n\n"
        "## Concepts\n\n"
        "- [[concepts/stale]] — stale"
    )
    old = f"Before.\n\n{legacy}\n\n# Skills\n\nAfter."
    current = build_wiki_context(CHINESE_INDEX)

    refreshed = refresh_wiki_prompt_block(old, current)

    assert "concepts/stale" not in refreshed
    assert refreshed.count(WIKI_BLOCK_START) == 1
    assert refreshed.count("# Knowledge Base") == 1
    assert refreshed == (
        f"Before.\n\n{WIKI_BLOCK_START}\n# Knowledge Base\n{current}\n"
        f"{WIKI_BLOCK_END}\n\n# Skills\n\nAfter."
    )


def test_refresh_collapses_mixed_legacy_and_marked_blocks() -> None:
    legacy = (
        "# Global Wiki\n\n"
        "The global Wiki is shared across all workspaces.\n"
        "Use Wiki search/read for durable knowledge.\n"
        "Propose only durable, sourced conclusions for writing.\n\n"
        "# Wiki Index\n\n- [[concepts/legacy]] — stale"
    )
    marked = f"{WIKI_BLOCK_START}\n# Global Wiki\nold\n{WIKI_BLOCK_END}"
    prompt = f"Before.\n\n{legacy}\n\nMiddle.\n\n{marked}\n\nAfter."

    refreshed = refresh_wiki_prompt_block(prompt, build_wiki_context(CHINESE_INDEX))

    assert refreshed.count(WIKI_BLOCK_START) == 1
    assert refreshed.count("# Knowledge Base") == 1
    assert "concepts/legacy" not in refreshed
    assert "Middle." in refreshed and "After." in refreshed


def test_refresh_migrates_repeated_legacy_blocks_without_deleting_adjacent_custom_content() -> None:
    legacy = (
        "# Global Wiki\n\n"
        "The global Wiki is shared across all workspaces.\n"
        "Use Wiki search/read for durable knowledge.\n"
        "Propose only durable, sourced conclusions for writing.\n\n"
        "# Wiki Index\n\n- [[concepts/legacy]] — stale"
    )
    custom = "## Custom Policy\n\nKeep this exact adjacent content."
    prompt = f"Before.\n\n{legacy}\n\n{custom}\n\n{legacy}\n\n# Skills\n\nAfter."

    refreshed = refresh_wiki_prompt_block(prompt, build_wiki_context(CHINESE_INDEX))

    assert refreshed.count(WIKI_BLOCK_START) == 1
    assert refreshed.count("# Knowledge Base") == 1
    assert custom in refreshed
    assert "Before." in refreshed and "# Skills\n\nAfter." in refreshed


def test_refresh_removes_stale_legacy_block_when_wiki_is_unavailable() -> None:
    legacy = (
        "# Global Wiki\n\n"
        "The global Wiki is shared across all workspaces.\n"
        "Use Wiki search/read for durable knowledge.\n"
        "Propose only durable, sourced conclusions for writing.\n\n"
        "# Wiki Index\n\n- [[concepts/legacy]] — stale"
    )
    prompt = f"Before.\n\n{legacy}\n\n# Skills\n\nAfter."

    refreshed = refresh_wiki_prompt_block(prompt, "")

    assert refreshed == "Before.\n\n\n\n# Skills\n\nAfter."
    assert "# Global Wiki" not in refreshed


def test_refresh_true_preupgrade_prompt_preserves_all_existing_content() -> None:
    prompt = "Before.\n\n# Custom Policy\n\nKeep me.\n\n# Skills\n\nAfter."

    refreshed = refresh_wiki_prompt_block(prompt, build_wiki_context(CHINESE_INDEX))
    without_block = (
        refreshed[: refreshed.index(WIKI_BLOCK_START)]
        + refreshed[refreshed.index(WIKI_BLOCK_END) + len(WIKI_BLOCK_END) :]
    )

    assert without_block == prompt


def test_refresh_collapses_duplicate_blocks_and_removes_stale_block_when_unavailable() -> None:
    block = f"{WIKI_BLOCK_START}\n# Global Wiki\nold\n{WIKI_BLOCK_END}"
    duplicated = f"Before.\n\n{block}\n\nMiddle.\n\n{block}\n\nAfter."

    refreshed = refresh_wiki_prompt_block(duplicated, build_wiki_context(CHINESE_INDEX))
    unavailable = refresh_wiki_prompt_block(duplicated, "")

    assert refreshed.count(WIKI_BLOCK_START) == 1
    assert refreshed.count(WIKI_BLOCK_END) == 1
    assert "Before." in refreshed and "Middle." in refreshed and "After." in refreshed
    assert WIKI_BLOCK_START not in unavailable
    assert "Before." in unavailable and "Middle." in unavailable and "After." in unavailable


def test_wiki_prompt_block_preserves_utf8_within_total_budget() -> None:
    large_index = "# Wiki Index\n\n" + "\n".join(
        f"- [[concepts/中文-{number}]] — 并发写入与故障恢复" for number in range(300)
    )

    context = build_wiki_context(large_index, hints=("中文-299",))
    prompt = refresh_wiki_prompt_block("Existing prompt.", context)
    block = prompt[prompt.index(WIKI_BLOCK_START) : prompt.index(WIKI_BLOCK_END)] + WIKI_BLOCK_END

    assert len(block.encode("utf-8")) <= WIKI_PROMPT_MAX_BYTES
    assert "中文-299" in block
    block.encode("utf-8").decode("utf-8")


def test_candidate_accepts_a_page_written_without_its_wrapper() -> None:
    """`pages: [{title, body, ...}]` is one level shallower than the schema.

    Seen in a real session: the errors it earned were "page: Field required"
    and "body: Extra input", which together never say "you are a level short".
    """
    from kimo_cli.wiki.models import WikiCandidate

    page = {
        "logical_path": "concepts/x.md",
        "title": "X",
        "created": "2026-09-23T00:00:00Z",
        "updated": "2026-09-23T00:00:00Z",
        "revision": 1,
        "body": "text\n",
        "expected_revision": 2,
    }

    candidate = WikiCandidate.model_validate({"summary": "s", "value": "high", "pages": [page]})

    assert candidate.pages[0].page.logical_path == "concepts/x.md"
    # Carried out of the page, where PageChange keeps it.
    assert candidate.pages[0].expected_revision == 2
    assert candidate.pages[0].page.body == "text\n"


def test_a_stand_in_content_hash_is_refused_by_name() -> None:
    from pydantic import ValidationError

    from kimo_cli.wiki.models import SourceRef

    with pytest.raises(ValidationError) as excinfo:
        SourceRef.model_validate({"kind": "conversation", "content_hash": "sha256:placeholder"})

    message = str(excinfo.value)
    assert "computed from the source bytes" in message
    assert "leave `sources` out" in message


def _candidate_payload() -> dict:
    return {
        "summary": "s",
        "value": "high",
        "pages": [
            {
                "page": {
                    "logical_path": "concepts/x.md",
                    "title": "X",
                    "created": "2026-09-23T00:00:00Z",
                    "updated": "2026-09-23T00:00:00Z",
                    "revision": 1,
                    "body": "text\n",
                }
            }
        ],
    }


def test_candidate_accepts_a_json_encoded_candidate() -> None:
    """A pre-serialized candidate is parsed instead of failing as ``model_type``.

    Seen in a real session: ``candidate`` arrived as a string containing JSON
    and earned "input should be a valid dictionary or instance of
    WikiCandidate", which named the wrapper it arrived in, not the fix.
    """
    import json

    from kimo_cli.wiki.models import WikiCandidate

    from_dict = WikiCandidate.model_validate(_candidate_payload())
    from_string = WikiCandidate.model_validate(json.dumps(_candidate_payload()))

    assert from_string == from_dict


def test_candidate_coercion_composes_with_the_page_wrapper() -> None:
    """Both shape slips at once still normalise.

    They live in one validator on purpose: Pydantic runs ``mode="before"``
    model validators outermost-first, so two siblings would each see the
    other's input untouched and the second slip would surface as an error
    neither one describes.
    """
    import json

    from kimo_cli.wiki.models import WikiCandidate

    page = {
        "logical_path": "concepts/x.md",
        "title": "X",
        "created": "2026-09-23T00:00:00Z",
        "updated": "2026-09-23T00:00:00Z",
        "revision": 1,
        "body": "text\n",
        "expected_revision": 2,
    }

    candidate = WikiCandidate.model_validate(
        json.dumps({"summary": "s", "value": "high", "pages": [page]})
    )

    assert candidate.pages[0].page.body == "text\n"
    assert candidate.pages[0].expected_revision == 2


def test_candidate_promotes_a_zero_revision_outside_the_wrapper() -> None:
    """0 is what a caller reaches for a page it is creating.

    ``WikiPage.revision`` stays ``PositiveInt`` because it also models stored
    pages, where 0 means corrupt frontmatter rather than "not born yet" -- see
    ``test_page_rejects_malformed_or_unsafe_content`` in test_schema.py.
    """
    from kimo_cli.wiki.models import WikiCandidate

    page = {
        "logical_path": "concepts/x.md",
        "title": "X",
        "created": "2026-09-23T00:00:00Z",
        "updated": "2026-09-23T00:00:00Z",
        "revision": 0,
        "body": "text\n",
    }

    candidate = WikiCandidate.model_validate({"summary": "s", "value": "high", "pages": [page]})

    assert candidate.pages[0].page.revision == 1


def test_candidate_promotes_a_zero_revision_inside_the_wrapper() -> None:
    from kimo_cli.wiki.models import WikiCandidate

    page = {
        "logical_path": "concepts/x.md",
        "title": "X",
        "created": "2026-09-23T00:00:00Z",
        "updated": "2026-09-23T00:00:00Z",
        "revision": 0,
        "body": "text\n",
    }

    candidate = WikiCandidate.model_validate(
        {"summary": "s", "value": "high", "pages": [{"page": page}]}
    )

    assert candidate.pages[0].page.revision == 1


def test_candidate_still_refuses_a_negative_revision() -> None:
    from kimo_cli.wiki.models import WikiCandidate

    with pytest.raises(Exception) as excinfo:
        WikiCandidate.model_validate(
            {
                "summary": "s",
                "value": "high",
                "pages": [
                    {
                        "page": {
                            "logical_path": "concepts/x.md",
                            "title": "X",
                            "created": "2026-09-23T00:00:00Z",
                            "updated": "2026-09-23T00:00:00Z",
                            "revision": -1,
                            "body": "text\n",
                        }
                    }
                ],
            }
        )

    assert excinfo.value.errors()[0]["type"] == "greater_than"


@pytest.mark.parametrize("text", ["42", '"hello"', "[1, 2]", "not json", ""])
def test_json_coercion_refuses_non_object_strings(text: str) -> None:
    """Only strings that parse to an object are coerced.

    A string that parses to a scalar, or that does not parse at all, is handed
    back untouched so the ordinary validator reports it.
    """
    from kimo_cli.wiki.models import WikiCandidate

    with pytest.raises(Exception) as excinfo:
        WikiCandidate.model_validate(text)

    assert excinfo.value.errors()[0]["type"] == "model_type"


def test_stored_page_still_refuses_a_zero_revision() -> None:
    """The read side stays strict: promotion is candidate-only."""
    from pydantic import ValidationError

    from kimo_cli.wiki.models import UnsafeWikiPage
    from kimo_cli.wiki.schema import parse_page

    page = (
        "---\n"
        "title: X\n"
        "created: 2026-09-23T00:00:00Z\n"
        "updated: 2026-09-23T00:00:00Z\n"
        "revision: 0\n"
        "---\n"
        "body text\n"
    )

    with pytest.raises((UnsafeWikiPage, ValidationError, ValueError)):
        parse_page(page, "concepts/x.md")


def test_current_source_accepts_a_json_encoded_source() -> None:
    import json

    from kimo_cli.wiki.models import CurrentSource

    source = {"kind": "inline", "content": "turn content"}

    assert CurrentSource.model_validate(json.dumps(source)) == CurrentSource.model_validate(source)
