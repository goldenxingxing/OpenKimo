"""What a compaction summary carries forward.

Each compaction used to summarise only what it was shown, and nothing told the model
that an earlier summary in that input was the only record of older work, so what it
held could quietly drop out a compaction later. Which files the session had read
or changed, and what the user last asked for, were left to the model to remember.

Now the prompt asks for earlier summaries to be merged, and the summary ends with
blocks computed from the tool calls themselves: files read, files modified (merged
across compactions, as pi does), and the latest user request when it is compacted
away (as openclaw does).
"""

from __future__ import annotations

import json

from kosong.chat_provider.mock import MockChatProvider
from kosong.message import Message, TextPart, ToolCall

from kimo_cli.llm import LLM
from kimo_cli.soul.compaction import LATEST_USER_REQUEST_MAX_CHARS, SimpleCompaction
from kimo_cli.soul.message import system, system_reminder


def _llm(summary: str = "SUMMARY") -> LLM:
    return LLM(
        chat_provider=MockChatProvider([TextPart(text=summary)]),
        max_context_size=200_000,
        capabilities=set(),
    )


def _tool_use(call_id: str, name: str, path: str) -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[TextPart(text=f"using {name}")],
            tool_calls=[
                ToolCall(
                    id=call_id,
                    function=ToolCall.FunctionBody(name=name, arguments=json.dumps({"path": path})),
                )
            ],
        ),
        Message(role="tool", tool_call_id=call_id, content=[TextPart(text="ok")]),
    ]


def _text(messages) -> str:  # noqa: ANN001
    return "\n".join(p.text for m in messages for p in m.content if isinstance(p, TextPart))


def _tail() -> list[Message]:
    return [
        Message(role="assistant", content=[TextPart(text="recent 1")]),
        Message(role="assistant", content=[TextPart(text="recent 2")]),
    ]


async def test_the_prompt_asks_to_merge_an_earlier_summary() -> None:
    messages = [
        Message(
            role="user",
            content=[system("Previous context has been compacted..."), TextPart(text="old")],
        ),
        *_tail(),
    ]

    prepared = SimpleCompaction().prepare(messages)

    assert prepared.compact_message is not None
    prompt = _text([prepared.compact_message])
    assert "earlier summary" in prompt.lower()
    assert "only record" in prompt.lower()


async def test_the_summary_lists_the_files_read_and_modified() -> None:
    messages = [
        Message(role="user", content=[TextPart(text="fix the parser")]),
        *_tool_use("1", "ReadFile", "/repo/parser.py"),
        *_tool_use("2", "ReadFile", "/repo/lexer.py"),
        *_tool_use("3", "StrReplaceFile", "/repo/parser.py"),
        *_tool_use("4", "WriteFile", "/repo/test_parser.py"),
        *_tool_use("5", "Grep", "/repo"),
        *_tail(),
    ]

    result = await SimpleCompaction().compact(messages, _llm())
    summary = _text(result.messages[:1])

    assert "SUMMARY" in summary
    assert "<files_read>\n- /repo/lexer.py\n</files_read>" in summary
    assert "- /repo/parser.py" in summary.split("<files_modified>")[1]
    assert "- /repo/test_parser.py" in summary.split("<files_modified>")[1]
    assert "/repo\n" not in summary  # Grep is not a file read


async def test_file_lists_are_merged_across_compactions() -> None:
    first = await SimpleCompaction().compact(
        [
            Message(role="user", content=[TextPart(text="start")]),
            *_tool_use("1", "ReadFile", "/repo/a.py"),
            *_tool_use("2", "WriteFile", "/repo/b.py"),
            *_tail(),
        ],
        _llm("FIRST"),
    )

    second = await SimpleCompaction().compact(
        [
            *first.messages,
            *_tool_use("3", "StrReplaceFile", "/repo/a.py"),
            *_tool_use("4", "ReadFile", "/repo/c.py"),
            *_tail(),
        ],
        _llm("SECOND"),
    )
    summary = _text(second.messages[:1])

    read = summary.split("<files_read>")[1].split("</files_read>")[0]
    modified = summary.split("<files_modified>")[1].split("</files_modified>")[0]
    assert "/repo/c.py" in read
    assert "/repo/a.py" not in read  # it was modified since
    assert "/repo/a.py" in modified
    assert "/repo/b.py" in modified


async def test_the_latest_user_request_survives_when_it_is_compacted_away() -> None:
    long_request = "please refactor the billing module " * 100
    messages = [
        Message(role="user", content=[TextPart(text="an older request")]),
        Message(role="user", content=[TextPart(text=long_request)]),
        Message(role="user", content=[system_reminder("injected, not the user")]),
        *_tool_use("1", "ReadFile", "/repo/billing.py"),
        *_tail(),
    ]

    result = await SimpleCompaction().compact(messages, _llm())
    summary = _text(result.messages[:1])

    block = summary.split("<latest_user_request>\n")[1].split("\n</latest_user_request>")[0]
    assert block.startswith("please refactor the billing module")
    assert len(block) <= LATEST_USER_REQUEST_MAX_CHARS + 1  # plus the ellipsis
    assert "injected" not in block

    # carried forward by a later compaction that sees no newer request
    later = await SimpleCompaction().compact([*result.messages, *_tail()], _llm("LATER"))
    later_summary = _text(later.messages[:1])
    carried = later_summary.split("<latest_user_request>\n")[1].split("\n</latest_user_request>")[0]
    assert carried == block  # the request itself, not the earlier summary
    assert later_summary.count("<latest_user_request>") == 1


async def test_a_request_still_in_the_kept_tail_is_not_repeated() -> None:
    messages = [
        *_tool_use("1", "ReadFile", "/repo/x.py"),
        Message(role="assistant", content=[TextPart(text="middle")]),
        Message(role="user", content=[TextPart(text="current question")]),
        Message(role="assistant", content=[TextPart(text="answer")]),
    ]

    result = await SimpleCompaction(max_preserved_messages=2).compact(messages, _llm())

    assert "<latest_user_request>" not in _text(result.messages[:1])
