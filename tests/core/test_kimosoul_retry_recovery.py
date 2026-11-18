from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Self
from unittest.mock import AsyncMock

import pytest
from kosong.chat_provider import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    StreamedMessagePart,
    ThinkingEffort,
    TokenUsage,
)
from kosong.message import Message, TextPart, ThinkPart
from kosong.tooling import Tool
from kosong.tooling.simple import SimpleToolset
from pydantic import SecretStr

from kimo_cli.config import LLMModel, LLMProvider, OAuthRef
from kimo_cli.llm import LLM
from kimo_cli.soul import run_soul
from kimo_cli.soul.agent import Agent, Runtime
from kimo_cli.soul.compaction import (
    COMPACTION_OUTPUT_PREFIX,
    COMPACTION_SYSTEM_PROMPT,
    estimate_text_tokens,
)
from kimo_cli.soul.context import Context
from kimo_cli.soul.kimosoul import KimoSoul
from kimo_cli.utils.aioqueue import QueueShutDown
from kimo_cli.wire import Wire
from kimo_cli.wire.types import StepBegin, StepRetry


class StaticStreamedMessage:
    def __init__(self, parts: Sequence[StreamedMessagePart]) -> None:
        self._iter = self._to_stream(parts)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        return await self._iter.__anext__()

    async def _to_stream(
        self, parts: Sequence[StreamedMessagePart]
    ) -> AsyncIterator[StreamedMessagePart]:
        for part in parts:
            yield part

    @property
    def id(self) -> str | None:
        return "recovering"

    @property
    def usage(self) -> TokenUsage | None:
        return None

    @property
    def trace_id(self) -> str | None:
        return None


class RecoveringSequenceProvider:
    name = "recovering-sequence"

    def __init__(self) -> None:
        self.generate_attempts = 0
        self.recovery_calls = 0

    @property
    def model_name(self) -> str:
        return "recovering-sequence"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage | PartialThenErrorStreamedMessage:
        self.generate_attempts += 1
        if self.generate_attempts == 1:
            raise APIConnectionError("Connection error.")
        return StaticStreamedMessage([TextPart(text="recovered")])

    def on_retryable_error(self, error: BaseException) -> bool:
        self.recovery_calls += 1
        return True

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class HungProvider:
    """A provider whose generate() never returns — the hung-LLM-call case."""

    name = "hung"

    def __init__(self) -> None:
        self.generate_attempts = 0
        self.recovery_calls = 0

    @property
    def model_name(self) -> str:
        return "hung"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        self.generate_attempts += 1
        await asyncio.Event().wait()  # hangs forever
        raise AssertionError("unreachable")

    def on_retryable_error(self, error: BaseException) -> bool:
        self.recovery_calls += 1
        return True

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class AlwaysConnectionErrorProvider:
    name = "always-connection-error"

    def __init__(self) -> None:
        self.generate_attempts = 0
        self.recovery_calls = 0

    @property
    def model_name(self) -> str:
        return "always-connection-error"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        self.generate_attempts += 1
        raise APIConnectionError("Connection error.")

    def on_retryable_error(self, error: BaseException) -> bool:
        self.recovery_calls += 1
        return True

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class StatusErrorThenSuccessProvider:
    name = "status-error-then-success"

    def __init__(self, status_code: int = 503) -> None:
        self.generate_attempts = 0
        self.recovery_calls = 0
        self._status_code = status_code

    @property
    def model_name(self) -> str:
        return "status-error-then-success"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        self.generate_attempts += 1
        if self.generate_attempts < 3:
            raise APIStatusError(self._status_code, f"Status {self._status_code}")
        return StaticStreamedMessage([TextPart(text="status recovered")])

    def on_retryable_error(self, error: BaseException) -> bool:
        self.recovery_calls += 1
        return True

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class PartialStreamThenStatusErrorProvider:
    name = "partial-stream-then-status-error"

    def __init__(self, status_code: int = 429) -> None:
        self.generate_attempts = 0
        self._status_code = status_code

    @property
    def model_name(self) -> str:
        return "partial-stream-then-status-error"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage | PartialThenErrorStreamedMessage:
        self.generate_attempts += 1
        if self.generate_attempts == 1:
            return PartialThenErrorStreamedMessage(
                [ThinkPart(think="old attempt")],
                APIStatusError(self._status_code, f"Status {self._status_code}"),
            )
        return StaticStreamedMessage([ThinkPart(think="new attempt"), TextPart(text="done")])

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class PartialThenErrorStreamedMessage:
    def __init__(self, parts: Sequence[StreamedMessagePart], error: BaseException) -> None:
        self._iter = self._to_stream(parts, error)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        return await self._iter.__anext__()

    async def _to_stream(
        self, parts: Sequence[StreamedMessagePart], error: BaseException
    ) -> AsyncIterator[StreamedMessagePart]:
        for part in parts:
            yield part
        raise error

    @property
    def id(self) -> str | None:
        return "partial-error"

    @property
    def usage(self) -> TokenUsage | None:
        return None

    @property
    def trace_id(self) -> str | None:
        return None


class NonRetryableConnectionProvider:
    name = "non-retryable-connection"

    def __init__(self) -> None:
        self.generate_attempts = 0

    @property
    def model_name(self) -> str:
        return "non-retryable-connection"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        self.generate_attempts += 1
        if self.generate_attempts == 1:
            raise APIConnectionError("Connection error.")
        return StaticStreamedMessage([TextPart(text="non-retryable recovered")])

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class ContextOverflowThenSuccessProvider:
    """Refuses the first step for exceeding the context window, then serves normally.

    Routes on the system prompt rather than a call counter: compaction also
    kicks off a fire-and-forget memory archivist that calls the LLM, and where
    its call lands in the sequence is not deterministic.
    """

    name = "context-overflow-then-success"

    def __init__(self, agent_prompt: str) -> None:
        self._agent_prompt = agent_prompt
        self.step_calls = 0
        self.compaction_calls = 0

    @property
    def model_name(self) -> str:
        return "context-overflow-then-success"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        if system_prompt == COMPACTION_SYSTEM_PROMPT:
            self.compaction_calls += 1
            return StaticStreamedMessage([TextPart(text="summary of the conversation")])
        if system_prompt != self._agent_prompt:
            # Background memory archivist; nothing to do with this test.
            return StaticStreamedMessage([TextPart(text="noted")])
        self.step_calls += 1
        if self.step_calls == 1:
            raise APIStatusError(400, "This model's maximum context length is 262144 tokens")
        return StaticStreamedMessage([TextPart(text="healed")])

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class ConnectionThen401ThenSuccessProvider:
    name = "connection-then-401-then-success"

    def __init__(self) -> None:
        self.generate_attempts = 0
        self.recovery_calls = 0

    @property
    def model_name(self) -> str:
        return "connection-then-401-then-success"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        self.generate_attempts += 1
        if self.generate_attempts == 1:
            raise APIConnectionError("Connection error.")
        if self.generate_attempts == 2:
            raise APIStatusError(401, "expired token")
        return StaticStreamedMessage([TextPart(text="auth recovered")])

    def on_retryable_error(self, error: BaseException) -> bool:
        self.recovery_calls += 1
        return True

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


def _runtime_with_llm(runtime: Runtime, llm: LLM) -> Runtime:
    return Runtime(
        config=runtime.config,
        llm=llm,
        session=runtime.session,
        builtin_args=runtime.builtin_args,
        denwa_renji=runtime.denwa_renji,
        approval=runtime.approval,
        labor_market=runtime.labor_market,
        environment=runtime.environment,
        notifications=runtime.notifications,
        background_tasks=runtime.background_tasks,
        skills=runtime.skills,
        oauth=runtime.oauth,
        additional_dirs=runtime.additional_dirs,
        skills_dirs=runtime.skills_dirs,
        role=runtime.role,
        user_memory_dir=runtime.user_memory_dir,
    )


_AGENT_SYSTEM_PROMPT = "Retry test prompt."


def _make_soul(runtime: Runtime, llm: LLM, tmp_path: Path) -> tuple[KimoSoul, Context]:
    agent = Agent(
        name="Retry Test Agent",
        system_prompt=_AGENT_SYSTEM_PROMPT,
        toolset=SimpleToolset(),
        runtime=_runtime_with_llm(runtime, llm),
    )
    context = Context(file_backend=tmp_path / "history.jsonl")
    return KimoSoul(agent, context=context), context


async def _drain_ui_messages(wire: Wire) -> None:
    wire_ui = wire.ui_side(merge=True)
    while True:
        try:
            await wire_ui.receive()
        except QueueShutDown:
            return


async def _collect_ui_messages(wire: Wire, seen: list[object]) -> None:
    wire_ui = wire.ui_side(merge=True)
    while True:
        try:
            seen.append(await wire_ui.receive())
        except QueueShutDown:
            return


@pytest.mark.asyncio
async def test_step_retry_recovers_retryable_provider(runtime: Runtime, tmp_path: Path) -> None:
    runtime.config.loop_control.max_retries_per_step = 2
    provider = RecoveringSequenceProvider()
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )
    soul, context = _make_soul(runtime, llm, tmp_path)

    await run_soul(soul, "trigger recovery", _drain_ui_messages, asyncio.Event())

    assert provider.generate_attempts == 2
    assert provider.recovery_calls == 1
    assert context.history[-1].extract_text(" ").strip() == "recovered"


@pytest.mark.asyncio
async def test_step_timeout_converts_hung_provider_to_retryable_error(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A provider call that hangs must hit the step timeout, not wait forever.

    The timeout surfaces as APITimeoutError (a ChatProviderError), so the
    existing retry machinery treats it like any retryable provider failure —
    and, with the retry budget at one attempt, the turn fails fast.
    """
    runtime.config.loop_control.max_retries_per_step = 1
    runtime.config.loop_control.step_timeout_s = 0.4
    provider = HungProvider()
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )
    soul, _ = _make_soul(runtime, llm, tmp_path)

    t0 = time.monotonic()
    with pytest.raises(APITimeoutError):
        await run_soul(soul, "trigger hang", _drain_ui_messages, asyncio.Event())
    elapsed = time.monotonic() - t0

    assert provider.generate_attempts == 1
    assert elapsed < 5  # failed fast, did not wait on the hung call


@pytest.mark.asyncio
async def test_step_connection_error_uses_full_retry_budget(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A network failure gets every attempt, with the transport rebuilt before each.

    Recovery used to retry inline and then mark the error unretryable, so a
    connection error consumed one attempt with no backoff and ended the turn
    while the rest of the budget went unused.
    """
    runtime.config.loop_control.max_retries_per_step = 3
    provider = AlwaysConnectionErrorProvider()
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )
    soul, _ = _make_soul(runtime, llm, tmp_path)

    with pytest.raises(APIConnectionError):
        await run_soul(soul, "trigger connection failure", _drain_ui_messages, asyncio.Event())

    assert provider.generate_attempts == 3
    assert provider.recovery_calls == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_code",
    # 408/409/529 were classified retryable for telemetry but not by the retry
    # loop, so an overloaded backend (529) ended the turn on the first refusal.
    [408, 429, 503, 504, 529],
    ids=["408", "429", "503", "504", "529"],
)
async def test_step_status_error_still_uses_tenacity_retries(
    runtime: Runtime, tmp_path: Path, status_code: int
) -> None:
    runtime.config.loop_control.max_retries_per_step = 3
    provider = StatusErrorThenSuccessProvider(status_code=status_code)
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )
    soul, context = _make_soul(runtime, llm, tmp_path)

    await run_soul(soul, "trigger status retry", _drain_ui_messages, asyncio.Event())

    assert provider.generate_attempts == 3
    assert provider.recovery_calls == 0
    assert context.history[-1].extract_text(" ").strip() == "status recovered"


@pytest.mark.asyncio
async def test_step_retry_event_after_partial_stream(runtime: Runtime, tmp_path: Path) -> None:
    runtime.config.loop_control.max_retries_per_step = 2
    provider = PartialStreamThenStatusErrorProvider(status_code=429)
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )
    soul, context = _make_soul(runtime, llm, tmp_path)
    seen: list[object] = []

    await run_soul(
        soul,
        "trigger streamed status retry",
        lambda wire: _collect_ui_messages(wire, seen),
        asyncio.Event(),
    )

    assert provider.generate_attempts == 2
    assert [type(msg) for msg in seen if isinstance(msg, StepBegin)] == [StepBegin]
    retry = next(msg for msg in seen if isinstance(msg, StepRetry))
    assert retry.n == 1
    assert retry.next_attempt == 2
    assert retry.max_attempts == 2
    assert retry.error_type == "APIStatusError"
    assert retry.status_code == 429
    parts = [msg for msg in seen if isinstance(msg, ThinkPart | TextPart)]
    assert parts == [
        ThinkPart(think="old attempt"),
        ThinkPart(think="new attempt"),
        TextPart(text="done"),
    ]
    assert context.history[-1].extract_text(" ").strip() == "done"


@pytest.mark.asyncio
async def test_step_non_retryable_provider_keeps_tenacity_connection_retries(
    runtime: Runtime, tmp_path: Path
) -> None:
    runtime.config.loop_control.max_retries_per_step = 2
    provider = NonRetryableConnectionProvider()
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )
    soul, context = _make_soul(runtime, llm, tmp_path)

    await run_soul(
        soul, "trigger non-retryable connection retry", _drain_ui_messages, asyncio.Event()
    )

    assert provider.generate_attempts == 2
    assert context.history[-1].extract_text(" ").strip() == "non-retryable recovered"


@pytest.mark.asyncio
async def test_context_overflow_compacts_and_retries_the_step(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A context-window refusal is the one failure the loop already knows how to fix.

    Auto-compaction runs off a local token estimate, so a turn can slip past the
    trigger and still be refused; that used to end the turn.
    """
    provider = ContextOverflowThenSuccessProvider(_AGENT_SYSTEM_PROMPT)
    llm = LLM(
        chat_provider=provider,
        max_context_size=1_000_000,
        capabilities=set(),
    )
    soul, context = _make_soul(runtime, llm, tmp_path)

    # Enough history that compaction genuinely reclaims something — the self-heal
    # refuses to retry when it doesn't.
    filler = "x" * 4_000
    await context.append_message(
        [
            Message(
                role="user" if i % 2 == 0 else "assistant",
                content=[TextPart(text=f"message {i} {filler}")],
            )
            for i in range(16)
        ]
    )
    tokens_before = estimate_text_tokens(context.history)

    await run_soul(soul, "trigger context overflow", _drain_ui_messages, asyncio.Event())

    # step (refused) -> compaction -> step (retried)
    assert provider.step_calls == 2
    assert provider.compaction_calls == 1
    assert context.history[-1].extract_text(" ").strip() == "healed"
    assert any(COMPACTION_OUTPUT_PREFIX in msg.extract_text(" ") for msg in context.history)
    assert estimate_text_tokens(context.history) < tokens_before


@pytest.mark.asyncio
async def test_step_connection_recovery_then_401_triggers_oauth_refresh(
    runtime: Runtime, tmp_path: Path
) -> None:
    oauth_provider = LLMProvider(
        type="openai_legacy",
        base_url="https://api.test/v1",
        api_key=SecretStr(""),
        oauth=OAuthRef(storage="file", key="oauth/openai"),
    )
    oauth_model = LLMModel(
        provider="managed:openai",
        model="gpt-4o",
        max_context_size=100_000,
    )
    runtime.config.providers[oauth_model.provider] = oauth_provider
    runtime.config.models["openai/gpt-4o"] = oauth_model

    provider = ConnectionThen401ThenSuccessProvider()
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
        model_config=oauth_model,
        provider_config=oauth_provider,
    )
    soul, context = _make_soul(runtime, llm, tmp_path)

    refresh_mock = AsyncMock()
    runtime.oauth.ensure_fresh = refresh_mock

    await run_soul(soul, "trigger mixed recovery", _drain_ui_messages, asyncio.Event())

    assert provider.generate_attempts == 3
    assert provider.recovery_calls == 1
    assert context.history[-1].extract_text(" ").strip() == "auth recovered"
    assert len(refresh_mock.await_args_list) == 2
    assert any(call.kwargs.get("force") is True for call in refresh_mock.await_args_list)


class RateLimitedProvider:
    """Answers 429 a given number of times (forever when ``failures`` is None)."""

    name = "rate-limited"

    def __init__(self, failures: int | None, status_code: int = 429) -> None:
        self.generate_attempts = 0
        self._failures = failures
        self._status_code = status_code

    @property
    def model_name(self) -> str:
        return "rate-limited"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        self.generate_attempts += 1
        if self._failures is None or self.generate_attempts <= self._failures:
            raise APIStatusError(self._status_code, f"Status {self._status_code}")
        return StaticStreamedMessage([TextPart(text="finally")])

    def on_retryable_error(self, error: BaseException) -> bool:
        return True

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    import kimo_cli.soul.kimosoul as kimosoul_module

    monkeypatch.setattr(kimosoul_module, "_retry_wait", lambda _state: 0)


@pytest.mark.asyncio
async def test_no_attempt_limit_waits_out_a_long_rate_limit(
    runtime: Runtime, tmp_path: Path, no_backoff: None
) -> None:
    """Past any fixed budget: twelve 429s, then an answer, and the reply is kept."""
    runtime.config.loop_control.max_retries_per_step = 0
    provider = RateLimitedProvider(failures=12)
    llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
    soul, context = _make_soul(runtime, llm, tmp_path)
    seen: list[object] = []

    await run_soul(
        soul, "keep going", lambda wire: _collect_ui_messages(wire, seen), asyncio.Event()
    )

    assert provider.generate_attempts == 13
    assert context.history[-1].extract_text(" ").strip() == "finally"
    retries = [message for message in seen if isinstance(message, StepRetry)]
    assert len(retries) == 12
    assert {retry.max_attempts for retry in retries} == {50}


@pytest.mark.asyncio
async def test_no_attempt_limit_still_fails_at_once_on_a_rejected_request(
    runtime: Runtime, tmp_path: Path, no_backoff: None
) -> None:
    """A request the provider refuses outright is never going to succeed."""
    runtime.config.loop_control.max_retries_per_step = 0
    provider = RateLimitedProvider(failures=None, status_code=400)
    llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
    soul, _ = _make_soul(runtime, llm, tmp_path)

    with pytest.raises(APIStatusError):
        await run_soul(soul, "bad request", _drain_ui_messages, asyncio.Event())

    assert provider.generate_attempts == 1


@pytest.mark.asyncio
async def test_stopping_interrupts_the_wait_before_the_next_attempt(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The stop button has to work mid-backoff, or "until stopped" is "forever"."""
    from kimo_cli.soul import RunCancelled

    runtime.config.loop_control.max_retries_per_step = 0
    provider = RateLimitedProvider(failures=None)
    llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
    soul, _ = _make_soul(runtime, llm, tmp_path)
    cancel = asyncio.Event()

    async def stop_on_first_retry(wire: Wire) -> None:
        wire_ui = wire.ui_side(merge=True)
        while True:
            try:
                message = await wire_ui.receive()
            except QueueShutDown:
                return
            if isinstance(message, StepRetry):
                cancel.set()

    started = asyncio.get_running_loop().time()
    with pytest.raises(RunCancelled):
        await run_soul(soul, "stop me", stop_on_first_retry, cancel)
    elapsed = asyncio.get_running_loop().time() - started

    # The first backoff alone is at least half a second; stopping must not wait it out.
    assert elapsed < 0.4
    assert provider.generate_attempts == 1
