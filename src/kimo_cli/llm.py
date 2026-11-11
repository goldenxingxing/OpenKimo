from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, cast, get_args

import yaml
from kosong.chat_provider import ChatProvider, StreamedMessage, ThinkingEffort
from kosong.message import (
    AudioURLPart,
    ImageURLPart,
    Message,
    TextPart,
    ThinkPart,
    VideoURLPart,
)
from kosong.tooling import Tool
from kosong.utils.aio import Callback, callback
from pydantic import SecretStr

from kimo_cli.utils.logging import logger

if TYPE_CHECKING:
    from kimo_cli.auth.oauth import OAuthManager
    from kimo_cli.config import Config, LLMModel, LLMProvider

type ProviderType = Literal[
    "openai_legacy",
    "openai_responses",
    "anthropic",
    "google_genai",  # for backward-compatibility, equals to `gemini`
    "gemini",
    "vertexai",
    "_echo",
    "_scripted_echo",
]

type ModelCapability = Literal["image_in", "video_in", "thinking", "always_thinking"]
ALL_MODEL_CAPABILITIES: set[ModelCapability] = set(get_args(ModelCapability.__value__))
DEFAULT_UNKNOWN_CONTEXT_COMPLETION_TOKENS = 32_000
DEFAULT_COMPLETION_TOKEN_SAFETY_MARGIN = 1_024
MEDIA_TOKEN_ESTIMATE = 2_000


@dataclass(slots=True)
class LLM:
    chat_provider: ChatProvider
    max_context_size: int
    capabilities: set[ModelCapability]
    model_config: LLMModel | None = None
    provider_config: LLMProvider | None = None

    @property
    def model_name(self) -> str:
        return self.chat_provider.model_name


def parse_llm_providers_env() -> list[dict[str, Any]] | None:
    """Parse ``LLM_PROVIDERS`` environment variable as a YAML list.

    Expected format (YAML string inside an env var)::

        LLM_PROVIDERS='
        - name: openai
          type: openai_legacy
          api_key: sk-...
          base_url: https://api.openai.com/v1
          model: gpt-4o
          max_context_size: 1000000
          capabilities:
            - thinking
            - image_in
        '

    Returns:
        A list of provider dictionaries, or ``None`` if the variable is not
        set or cannot be parsed.
    """
    raw = os.environ.get("LLM_PROVIDERS")
    if not raw:
        return None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse LLM_PROVIDERS as YAML: {exc}", exc=exc)
        return None
    if not isinstance(data, list):
        logger.warning("LLM_PROVIDERS must be a YAML list, got {type}", type=type(data).__name__)
        return None
    return cast(list[dict[str, Any]], data)


@dataclass(slots=True)
class _TraceCallbackChatProvider:
    _provider: ChatProvider
    _on_trace_id: Callback[[str | None], None]
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = self._provider.name

    @property
    def model_name(self) -> str:
        return self._provider.model_name

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return self._provider.thinking_effort

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StreamedMessage:
        await callback(self._on_trace_id, None)
        try:
            stream = await self._provider.generate(system_prompt, tools, history)
        except BaseException as error:
            if trace_id := getattr(error, "trace_id", None):
                await callback(self._on_trace_id, trace_id)
            raise
        await callback(self._on_trace_id, getattr(stream, "trace_id", None))
        return stream

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return type(self)(self._provider.with_thinking(effort), self._on_trace_id)


def with_trace_callback(
    chat_provider: ChatProvider,
    on_trace_id: Callback[[str | None], None],
) -> ChatProvider:
    return _TraceCallbackChatProvider(chat_provider, on_trace_id)


def estimate_request_tokens(
    system_prompt: str,
    tools: Sequence[Tool],
    history: Sequence[Message],
) -> int:
    """Estimate all token-bearing parts of a chat request.

    The estimate is deliberately request-scoped: unlike ``Context.token_count_with_pending``,
    it includes the system prompt, tool schemas, message metadata, tool calls, and media. Exact
    tokenization is provider/model specific, so callers should still reserve a small safety
    margin when using the result to derive a hard completion limit.
    """
    return (
        _estimate_text_tokens(system_prompt)
        + sum(_estimate_tool_tokens(tool) for tool in tools)
        + sum(_estimate_message_tokens(message) for message in history)
    )


def estimate_message_tokens(messages: Sequence[Message]) -> int:
    """Estimate token-bearing content for messages added outside the main context."""
    return sum(_estimate_message_tokens(message) for message in messages)


def _estimate_text_tokens(text: str) -> int:
    ascii_count = sum(char.isascii() for char in text)
    non_ascii_count = len(text) - ascii_count
    return (ascii_count + 3) // 4 + non_ascii_count


def _estimate_tool_tokens(tool: Tool) -> int:
    return (
        _estimate_text_tokens(tool.name)
        + _estimate_text_tokens(tool.description)
        + _estimate_text_tokens(
            json.dumps(tool.parameters, ensure_ascii=False, separators=(",", ":"))
        )
    )


def _estimate_message_tokens(message: Message) -> int:
    total = _estimate_text_tokens(message.role)
    if message.name:
        total += _estimate_text_tokens(message.name)
    if message.tool_call_id:
        total += _estimate_text_tokens(message.tool_call_id)

    for part in message.content:
        if isinstance(part, TextPart):
            total += _estimate_text_tokens(part.text)
        elif isinstance(part, ThinkPart):
            total += _estimate_text_tokens(part.think)
        elif isinstance(part, (ImageURLPart, AudioURLPart, VideoURLPart)):
            total += MEDIA_TOKEN_ESTIMATE
        else:
            total += _estimate_text_tokens(part.model_dump_json(exclude_none=True))

    for tool_call in message.tool_calls or ():
        total += _estimate_text_tokens(tool_call.id)
        total += _estimate_text_tokens(tool_call.function.name)
        total += _estimate_text_tokens(tool_call.function.arguments or "")
        if tool_call.extras:
            total += _estimate_text_tokens(
                json.dumps(tool_call.extras, ensure_ascii=False, separators=(",", ":"))
            )
    return total


def model_display_name(model_name: str | None, model: LLMModel | None = None) -> str:
    if model is not None and model.display_name:
        return model.display_name
    if not model_name:
        return ""
    return model_name


def augment_provider_with_env_vars(provider: LLMProvider, model: LLMModel) -> dict[str, str]:
    """Override provider/model settings from environment variables.

    Returns:
        Mapping of environment variables that were applied.
    """
    applied: dict[str, str] = {}

    match provider.type:
        case "openai_legacy" | "openai_responses":
            # Only fill missing values from env vars; do not override
            # explicit config.toml settings (e.g. local providers)
            if not provider.base_url and (base_url := os.getenv("OPENAI_BASE_URL")):
                provider.base_url = base_url
            if not provider.api_key.get_secret_value() and (api_key := os.getenv("OPENAI_API_KEY")):
                provider.api_key = SecretStr(api_key)
            if model_name := os.getenv("OPENAI_MODEL_NAME"):
                model.model = model_name
                applied["OPENAI_MODEL_NAME"] = model_name
        case "anthropic":
            if not provider.base_url and (base_url := os.getenv("ANTHROPIC_BASE_URL")):
                provider.base_url = base_url
            if not provider.api_key.get_secret_value() and (
                api_key := os.getenv("ANTHROPIC_API_KEY")
            ):
                provider.api_key = SecretStr(api_key)
            if model_name := os.getenv("ANTHROPIC_MODEL_NAME"):
                model.model = model_name
                applied["ANTHROPIC_MODEL_NAME"] = model_name
        case _:
            pass

    # Model capabilities can be overridden for any provider
    if capabilities := os.getenv("KIMO_MODEL_CAPABILITIES"):
        caps_lower = (cap.strip().lower() for cap in capabilities.split(",") if cap.strip())
        model.capabilities = set(
            cast(ModelCapability, cap)
            for cap in caps_lower
            if cap in get_args(ModelCapability.__value__)
        )
        applied["KIMO_MODEL_CAPABILITIES"] = capabilities

    return applied


def create_llm(
    provider: LLMProvider,
    model: LLMModel,
    *,
    thinking: bool | None = None,
    session_id: str | None = None,
    oauth: OAuthManager | None = None,
) -> LLM | None:
    if provider.type not in {"_echo", "_scripted_echo"} and (
        not provider.base_url or not model.model
    ):
        logger.warning(
            "Cannot create LLM: missing base_url or model (provider_type={provider_type})",
            provider_type=provider.type,
        )
        return None

    resolved_api_key = (
        oauth.resolve_api_key(provider.api_key, provider.oauth)
        if oauth and provider.oauth
        else provider.api_key.get_secret_value()
    )

    match provider.type:
        case "openai_legacy":
            from kosong.contrib.chat_provider.openai_legacy import OpenAILegacy

            # None lets the provider use its default candidate keys
            # ("reasoning_content", "reasoning"); an explicit value — including
            # the empty string, which disables extraction — is passed through.
            chat_provider = OpenAILegacy(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                reasoning_key=provider.reasoning_key,
                default_headers=dict(provider.custom_headers) if provider.custom_headers else None,
            )
        case "openai_responses":
            from kosong.contrib.chat_provider.openai_responses import OpenAIResponses

            chat_provider = OpenAIResponses(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                default_headers=dict(provider.custom_headers) if provider.custom_headers else None,
            )
        case "anthropic":
            from kosong.contrib.chat_provider.anthropic import Anthropic

            chat_provider = Anthropic(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                default_max_tokens=50000,
                metadata={"user_id": session_id} if session_id else None,
                default_headers=dict(provider.custom_headers) if provider.custom_headers else None,
            )
        case "google_genai" | "gemini":
            from kosong.contrib.chat_provider.google_genai import GoogleGenAI

            chat_provider = GoogleGenAI(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                default_headers=dict(provider.custom_headers) if provider.custom_headers else None,
            )
        case "vertexai":
            from kosong.contrib.chat_provider.google_genai import GoogleGenAI

            os.environ.update(provider.env or {})
            chat_provider = GoogleGenAI(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                vertexai=True,
                default_headers=dict(provider.custom_headers) if provider.custom_headers else None,
            )
        case "_echo":
            from kosong.chat_provider.echo import EchoChatProvider

            chat_provider = EchoChatProvider()
        case "_scripted_echo":
            from kosong.chat_provider.echo import ScriptedEchoChatProvider

            if provider.env:
                os.environ.update(provider.env)
            scripts = _load_scripted_echo_scripts()
            trace_value = os.getenv("KIMO_SCRIPTED_ECHO_TRACE", "")
            trace = trace_value.strip().lower() in {"1", "true", "yes", "on"}
            chat_provider = ScriptedEchoChatProvider(scripts, trace=trace)

    capabilities = derive_model_capabilities(model)

    # Apply thinking if specified or if model always requires thinking
    thinking_on = "always_thinking" in capabilities or (
        thinking is True and "thinking" in capabilities
    )
    if thinking_on:
        chat_provider = chat_provider.with_thinking("high")
    elif thinking is False:
        chat_provider = chat_provider.with_thinking("off")
    # If thinking is None and model doesn't always think, leave as-is (default behavior)

    return LLM(
        chat_provider=chat_provider,
        max_context_size=model.max_context_size,
        capabilities=capabilities,
        model_config=model,
        provider_config=provider,
    )


def clone_llm_with_model_alias(
    llm: LLM | None,
    config: Config,
    model_alias: str | None,
    *,
    session_id: str,
    oauth: OAuthManager | None,
) -> LLM | None:
    if model_alias is None:
        return llm
    if model_alias not in config.models:
        raise KeyError(f"Unknown model alias: {model_alias}")
    model = config.models[model_alias]
    provider = config.providers[model.provider]
    thinking: bool | None = None
    if llm is not None:
        effort = getattr(llm.chat_provider, "thinking_effort", None)
        if effort is not None:
            thinking = effort != "off"
    return create_llm(
        provider,
        model,
        thinking=thinking,
        session_id=session_id,
        oauth=oauth,
    )


def derive_model_capabilities(model: LLMModel) -> set[ModelCapability]:
    capabilities = set(model.capabilities or ())
    # Models with "thinking" in their name are always-thinking models
    if "thinking" in model.model.lower() or "reason" in model.model.lower():
        capabilities.update(("thinking", "always_thinking"))
    # These models support thinking but can be toggled on/off
    # OpenAI GPT-4o and o1/o3 series support vision
    if any(name in model.model.lower() for name in ("gpt-4o", "o1", "o3")):
        capabilities.add("image_in")
    # Anthropic Claude 3/3.5/3.7 supports vision
    if "claude-3" in model.model.lower():
        capabilities.add("image_in")
    return capabilities


def _load_scripted_echo_scripts() -> list[str]:
    script_path = os.getenv("KIMO_SCRIPTED_ECHO_SCRIPTS")
    if not script_path:
        raise ValueError("KIMO_SCRIPTED_ECHO_SCRIPTS is required for _scripted_echo.")
    path = Path(script_path).expanduser()
    if not path.exists():
        raise ValueError(f"Scripted echo file not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        data: object = json.loads(text)
    except json.JSONDecodeError:
        scripts = [chunk.strip() for chunk in text.split("\n---\n") if chunk.strip()]
        if scripts:
            return scripts
        raise ValueError(
            "Scripted echo file must be a JSON array of strings or a text file "
            "split by '\\n---\\n'."
        ) from None
    if isinstance(data, list):
        data_list = cast(list[object], data)
        if all(isinstance(item, str) for item in data_list):
            return cast(list[str], data_list)
    raise ValueError("Scripted echo JSON must be an array of strings.")
