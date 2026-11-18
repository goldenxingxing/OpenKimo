"""Snapshot tests for OpenAI Legacy (Chat Completions API) chat provider."""

import json

import respx
from common import COMMON_CASES, Case, make_chat_completion_response, run_test_cases
from httpx import Response
from inline_snapshot import snapshot

from kosong.contrib.chat_provider.openai_legacy import OpenAILegacy
from kosong.message import Message, TextPart, ThinkPart

TEST_CASES: dict[str, Case] = {**COMMON_CASES}


async def test_openai_legacy_message_conversion():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response("gpt-4.1"))
        )
        provider = OpenAILegacy(model="gpt-4.1", api_key="test-key", stream=False)
        results = await run_test_cases(mock, provider, TEST_CASES, ("messages", "tools"))

        assert results == snapshot(
            {
                "simple_user_message": {
                    "messages": [
                        {"role": "system", "content": "You are helpful."},
                        {"role": "user", "content": "Hello!"},
                    ],
                    "tools": [],
                },
                "multi_turn_conversation": {
                    "messages": [
                        {"role": "user", "content": "What is 2+2?"},
                        {"role": "assistant", "content": "2+2 equals 4."},
                        {"role": "user", "content": "And 3+3?"},
                    ],
                    "tools": [],
                },
                "multi_turn_with_system": {
                    "messages": [
                        {"role": "system", "content": "You are a math tutor."},
                        {"role": "user", "content": "What is 2+2?"},
                        {"role": "assistant", "content": "2+2 equals 4."},
                        {"role": "user", "content": "And 3+3?"},
                    ],
                    "tools": [],
                },
                "image_url": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "What's in this image?"},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "https://example.com/image.png",
                                        "id": None,
                                    },
                                },
                            ],
                        }
                    ],
                    "tools": [],
                },
                "tool_definition": {
                    "messages": [{"role": "user", "content": "Add 2 and 3"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "add",
                                "description": "Add two integers.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "a": {
                                            "type": "integer",
                                            "description": "First number",
                                        },
                                        "b": {
                                            "type": "integer",
                                            "description": "Second number",
                                        },
                                    },
                                    "required": ["a", "b"],
                                },
                            },
                        },
                        {
                            "type": "function",
                            "function": {
                                "name": "multiply",
                                "description": "Multiply two integers.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "a": {"type": "integer", "description": "First number"},
                                        "b": {"type": "integer", "description": "Second number"},
                                    },
                                    "required": ["a", "b"],
                                },
                            },
                        },
                    ],
                },
                "tool_call_with_image": {
                    "messages": [
                        {"role": "user", "content": "Add 2 and 3"},
                        {
                            "role": "assistant",
                            "content": "I'll add those numbers for you.",
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "id": "call_abc123",
                                    "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'},
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "content": [
                                {"type": "text", "text": "5"},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "https://example.com/image.png",
                                        "id": None,
                                    },
                                },
                            ],
                            "tool_call_id": "call_abc123",
                        },
                    ],
                    "tools": [],
                },
                "tool_call": {
                    "messages": [
                        {"role": "user", "content": "Add 2 and 3"},
                        {
                            "role": "assistant",
                            "content": "I'll add those numbers for you.",
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "id": "call_abc123",
                                    "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'},
                                }
                            ],
                        },
                        {"role": "tool", "content": "5", "tool_call_id": "call_abc123"},
                    ],
                    "tools": [],
                },
                "parallel_tool_calls": {
                    "messages": [
                        {"role": "user", "content": "Calculate 2+3 and 4*5"},
                        {
                            "role": "assistant",
                            "content": "I'll calculate both.",
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "id": "call_add",
                                    "function": {
                                        "name": "add",
                                        "arguments": '{"a": 2, "b": 3}',
                                    },
                                },
                                {
                                    "type": "function",
                                    "id": "call_mul",
                                    "function": {
                                        "name": "multiply",
                                        "arguments": '{"a": 4, "b": 5}',
                                    },
                                },
                            ],
                        },
                        {
                            "role": "tool",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "<system-reminder>This is a system reminder"
                                    "</system-reminder>",
                                },
                                {"type": "text", "text": "5"},
                            ],
                            "tool_call_id": "call_add",
                        },
                        {
                            "role": "tool",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "<system-reminder>This is a system reminder"
                                    "</system-reminder>",
                                },
                                {"type": "text", "text": "20"},
                            ],
                            "tool_call_id": "call_mul",
                        },
                    ],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "add",
                                "description": "Add two integers.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "a": {"type": "integer", "description": "First number"},
                                        "b": {"type": "integer", "description": "Second number"},
                                    },
                                    "required": ["a", "b"],
                                },
                            },
                        },
                        {
                            "type": "function",
                            "function": {
                                "name": "multiply",
                                "description": "Multiply two integers.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "a": {"type": "integer", "description": "First number"},
                                        "b": {"type": "integer", "description": "Second number"},
                                    },
                                    "required": ["a", "b"],
                                },
                            },
                        },
                    ],
                },
            }
        )


async def test_openai_legacy_reasoning_content():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(
            model="deepseek-reasoner",
            api_key="test-key",
            stream=False,
            reasoning_key="reasoning_content",
        )
        history = [
            Message(role="user", content="What is 2+2?"),
            Message(
                role="assistant",
                content=[ThinkPart(think="Thinking..."), TextPart(text="4.")],
            ),
            Message(role="user", content="Thanks!"),
        ]
        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert body["messages"] == snapshot(
            [
                {"role": "user", "content": "What is 2+2?"},
                {
                    "role": "assistant",
                    "content": "4.",
                    "reasoning_content": "Thinking...",
                },
                {"role": "user", "content": "Thanks!"},
            ]
        )


async def test_openai_legacy_empty_reasoning_content_is_round_tripped():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(
            model="deepseek-reasoner",
            api_key="test-key",
            stream=False,
            reasoning_key="reasoning_content",
        )
        history = [
            Message(role="user", content="What is 2+2?"),
            Message(
                role="assistant",
                content=[ThinkPart(think=""), TextPart(text="4.")],
            ),
            Message(role="user", content="Thanks!"),
        ]
        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert body["messages"] == [
            {"role": "user", "content": "What is 2+2?"},
            {
                "role": "assistant",
                "content": "4.",
                "reasoning_content": "",
            },
            {"role": "user", "content": "Thanks!"},
        ]


async def test_openai_legacy_generation_kwargs():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(
            model="gpt-4.1", api_key="test-key", stream=False
        ).with_generation_kwargs(temperature=0.7, max_tokens=2048)
        stream = await provider.generate("", [], [Message(role="user", content="Hi")])
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert (body["temperature"], body["max_tokens"]) == snapshot((0.7, 2048))


async def test_openai_legacy_with_thinking():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(model="gpt-4.1", api_key="test-key", stream=False).with_thinking(
            "high"
        )
        stream = await provider.generate("", [], [Message(role="user", content="Think")])
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert body["reasoning_effort"] == snapshot("high")


async def test_openai_legacy_auto_reasoning_effort_when_history_has_think_part():
    """When reasoning_effort is not set but history contains ThinkPart and reasoning_key is
    configured, reasoning_effort should be auto-set to avoid server validation errors.

    Reproduces: https://github.com/MoonshotAI/kimi-cli/issues/1616
    """
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        # Provider with reasoning_key but NO explicit reasoning_effort
        provider = OpenAILegacy(
            model="kimi-k2.5",
            api_key="test-key",
            stream=False,
            reasoning_key="reasoning_content",
        )
        history = [
            Message(role="user", content="Hello"),
            Message(
                role="assistant",
                content=[ThinkPart(think="Let me think..."), TextPart(text="Hi!")],
            ),
            Message(role="user", content="How are you?"),
        ]
        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        # reasoning_effort should be auto-set because history contains ThinkPart
        assert body["reasoning_effort"] == "medium"
        # reasoning_content should still be present in the message
        assert body["messages"][1]["reasoning_content"] == "Let me think..."


async def test_openai_legacy_no_auto_reasoning_effort_without_think_part():
    """When history has no ThinkPart, reasoning_effort should remain unset."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(
            model="kimi-k2.5",
            api_key="test-key",
            stream=False,
            reasoning_key="reasoning_content",
        )
        history = [
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi!"),
            Message(role="user", content="How are you?"),
        ]
        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert "reasoning_effort" not in body


async def test_openai_legacy_no_auto_reasoning_effort_with_empty_reasoning_key():
    """When reasoning_key is explicitly empty, reasoning_effort should not be auto-set
    even if history has ThinkPart (ThinkPart would be silently dropped)."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        # Explicitly empty reasoning_key disables reasoning handling entirely
        provider = OpenAILegacy(
            model="some-model",
            api_key="test-key",
            stream=False,
            reasoning_key="",
        )
        history = [
            Message(role="user", content="Hello"),
            Message(
                role="assistant",
                content=[ThinkPart(think="Thinking..."), TextPart(text="Hi!")],
            ),
            Message(role="user", content="How are you?"),
        ]
        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert "reasoning_effort" not in body


# ── multi-field reasoning extraction ──────────────────────────────────────────


def _make_reasoning_response(field: str, value: str) -> dict:
    return {
        "id": "chatcmpl-reasoning",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "2", field: value},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


async def test_openai_legacy_reads_reasoning_content_by_default():
    """DeepSeek / Kimo style: message.reasoning_content → ThinkPart."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(
                200, json=_make_reasoning_response("reasoning_content", "DeepSeek thinks...")
            )
        )
        provider = OpenAILegacy(model="deepseek-reasoner", api_key="test-key", stream=False)
        stream = await provider.generate("", [], [Message(role="user", content="1+1?")])
        parts = [part async for part in stream]
        think_parts = [p for p in parts if isinstance(p, ThinkPart)]
        assert len(think_parts) == 1
        assert think_parts[0].think == "DeepSeek thinks..."


async def test_openai_legacy_reads_reasoning_field_by_default():
    """SenseNova / MiMo style: message.reasoning → ThinkPart (no explicit reasoning_key needed)."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(
                200, json=_make_reasoning_response("reasoning", "SenseNova reasons...")
            )
        )
        provider = OpenAILegacy(model="sensenova-6.8-flash-lite", api_key="test-key", stream=False)
        stream = await provider.generate("", [], [Message(role="user", content="1+1?")])
        parts = [part async for part in stream]
        think_parts = [p for p in parts if isinstance(p, ThinkPart)]
        assert len(think_parts) == 1
        assert think_parts[0].think == "SenseNova reasons..."


async def test_openai_legacy_reasoning_content_takes_priority_over_reasoning():
    """When both fields are present, reasoning_content (the de-facto standard) wins."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(
                200,
                json={
                    "id": "chatcmpl-both",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "2",
                                "reasoning_content": "standard field",
                                "reasoning": "non-standard field",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            )
        )
        provider = OpenAILegacy(model="some-model", api_key="test-key", stream=False)
        stream = await provider.generate("", [], [Message(role="user", content="1+1?")])
        parts = [part async for part in stream]
        think_parts = [p for p in parts if isinstance(p, ThinkPart)]
        assert len(think_parts) == 1
        assert think_parts[0].think == "standard field"


async def test_openai_legacy_custom_reasoning_key_list():
    """A tuple/list of candidate keys can be supplied; first non-empty wins."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(
                200, json=_make_reasoning_response("custom_reasoning", "custom gateway")
            )
        )
        provider = OpenAILegacy(
            model="custom-model",
            api_key="test-key",
            stream=False,
            reasoning_key=("reasoning_content", "custom_reasoning"),
        )
        stream = await provider.generate("", [], [Message(role="user", content="1+1?")])
        parts = [part async for part in stream]
        think_parts = [p for p in parts if isinstance(p, ThinkPart)]
        assert len(think_parts) == 1
        assert think_parts[0].think == "custom gateway"


async def test_openai_legacy_empty_reasoning_key_disables_extraction():
    """reasoning_key=\"\" disables reasoning extraction entirely (existing behaviour)."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(
                200, json=_make_reasoning_response("reasoning_content", "should be dropped")
            )
        )
        provider = OpenAILegacy(
            model="no-reasoning-model",
            api_key="test-key",
            stream=False,
            reasoning_key="",
        )
        stream = await provider.generate("", [], [Message(role="user", content="1+1?")])
        parts = [part async for part in stream]
        think_parts = [p for p in parts if isinstance(p, ThinkPart)]
        assert len(think_parts) == 0


async def test_openai_legacy_auto_reasoning_effort_still_works_with_default_keys():
    """Auto reasoning_effort when history has ThinkPart + default multi-key (no explicit key)."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(
            model="kimi-k2.5",
            api_key="test-key",
            stream=False,
        )
        history = [
            Message(role="user", content="Hello"),
            Message(
                role="assistant",
                content=[ThinkPart(think="Let me think..."), TextPart(text="Hi!")],
            ),
            Message(role="user", content="How are you?"),
        ]
        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert body["reasoning_effort"] == "medium"
        assert body["messages"][1]["reasoning_content"] == "Let me think..."
