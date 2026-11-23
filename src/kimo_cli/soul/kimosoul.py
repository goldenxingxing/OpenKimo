from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import kosong
import tenacity
from kosong import StepResult
from kosong.chat_provider import (
    APIConnectionError,
    APIEmptyResponseError,
    APIStatusError,
    APITimeoutError,
    ChatProviderError,
    RetryableChatProvider,
)
from kosong.message import Message, ThinkPart, ToolCall
from kosong.tooling import ToolError
from kosong.tooling.empty import EmptyToolset
from tenacity import (
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from kimo_cli.approval_runtime import (
    ApprovalSource,
    get_current_approval_source_or_none,
    reset_current_approval_source,
    set_current_approval_source,
)
from kimo_cli.background import build_active_task_snapshot
from kimo_cli.hooks.engine import HookEngine
from kimo_cli.llm import (
    LLM,
    ModelCapability,
    estimate_request_tokens,
    with_trace_callback,
)
from kimo_cli.notifications import (
    NotificationView,
    build_notification_message,
    extract_notification_ids,
)
from kimo_cli.session_state import TodoItemState
from kimo_cli.skill import Skill, read_skill_text
from kimo_cli.skill.flow import Flow, FlowEdge, FlowNode, parse_choice
from kimo_cli.soul import (
    LLMNotSet,
    LLMNotSupported,
    MaxStepsReached,
    Soul,
    StatusSnapshot,
    wire_send,
)
from kimo_cli.soul.agent import Agent, Runtime
from kimo_cli.soul.compaction import (
    COMPACTION_OUTPUT_PREFIX,
    Compaction,
    CompactionResult,
    SimpleCompaction,
    estimate_message_bytes,
    estimate_text_tokens,
    should_auto_compact,
)
from kimo_cli.soul.context import Context
from kimo_cli.soul.dynamic_injection import (
    DynamicInjection,
    DynamicInjectionProvider,
    normalize_history,
)
from kimo_cli.soul.dynamic_injections.afk_mode import AfkModeInjectionProvider
from kimo_cli.soul.dynamic_injections.cross_session_memory import (
    CrossSessionMemoryInjectionProvider,
)
from kimo_cli.soul.dynamic_injections.plan_mode import PlanModeInjectionProvider
from kimo_cli.soul.dynamic_injections.session_memory import SessionMemoryInjectionProvider
from kimo_cli.soul.dynamic_injections.todo_sync import TodoSyncInjectionProvider
from kimo_cli.soul.evolution import EvolutionRecorder, EvolutionTurnRecord
from kimo_cli.soul.fs_checkpoint import FsCheckpointer, workspace_from_env
from kimo_cli.soul.history_repair import repair_tool_pairing
from kimo_cli.soul.loop_detection import LoopDetector
from kimo_cli.soul.message import (
    check_message,
    sanitize_image_parts,
    strip_image_parts,
    system,
    system_reminder,
    tool_result_to_message,
)
from kimo_cli.soul.slash import registry as soul_slash_registry
from kimo_cli.soul.tool_output_prune import ToolOutputPruner
from kimo_cli.soul.toolset import KimoToolset
from kimo_cli.tools.dmail import NAME as SendDMail_NAME
from kimo_cli.tools.utils import ToolRejectedError
from kimo_cli.utils.logging import logger
from kimo_cli.utils.slashcmd import SlashCommand, parse_slash_command_call
from kimo_cli.wire.file import WireFile
from kimo_cli.wire.types import (
    CompactionBegin,
    CompactionEnd,
    CompactionRetry,
    ContentPart,
    MCPLoadingBegin,
    MCPLoadingEnd,
    StatusUpdate,
    SteerInput,
    StepBegin,
    StepInterrupted,
    StepRetry,
    TextPart,
    ToolResult,
    TurnBegin,
    TurnEnd,
)

if TYPE_CHECKING:
    from kimo_cli.wiki.triggers import WikiCheckpoint

    def type_check(soul: KimoSoul):
        _: Soul = soul


SKILL_COMMAND_PREFIX = "skill:"
FLOW_COMMAND_PREFIX = "flow:"
DEFAULT_MAX_FLOW_MOVES = 1000


async def _await_shielded[T](awaitable: Awaitable[T]) -> T:
    """Wait for the inner task after outer cancellation, preserving its errors."""
    inner = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(inner)
    except asyncio.CancelledError:
        # If the inner failed while the outer task was being cancelled, awaiting
        # it raises that failure; otherwise the original cancellation is reraised.
        await inner
        raise


MAX_TRUNCATION_CONTINUATIONS = 3
"""How many times in a row a reply cut off by the output token limit is continued
before the turn ends with what it has."""

_TRUNCATION_CONTINUE_PROMPT = (
    "Your previous reply was cut off by the output token limit. Continue exactly where "
    "it stopped, without repeating what you already wrote."
)

FINALIZATION_PROMPT = (
    "You have reached the maximum number of steps allowed for this turn. Do not call "
    "any more tools. Briefly summarize: (1) what you completed, (2) what remains "
    "unfinished and why, (3) your final answer or recommendation."
)


#: How providers word "the prompt is longer than the context window". Each one
#: phrases it differently, and a provider missing here never gets the
#: compact-and-retry self-heal. Not "max tokens": that is also how a request
#: with a bad ``max_tokens`` value is refused, which compaction cannot fix.
_CONTEXT_OVERFLOW_MARKERS = (
    "context length",  # OpenAI chat completions, vLLM, DeepSeek
    "context_length",  # OpenAI error code context_length_exceeded
    "maximum context",
    "context window",  # OpenAI Responses
    "too many tokens",
    "model token limit",  # Kimo / Moonshot
    "prompt is too long",  # Anthropic
    "input token count",  # Gemini
)


def classify_api_error(e: Exception) -> tuple[str, int | None]:
    """Classify an LLM API exception into (error_type, status_code).

    Exposed at module level so telemetry tests can import the real function
    instead of duplicating the classification table.

    Returns:
        (error_type, status_code) where status_code is None for non-HTTP errors.
    """
    status_code: int | None = None
    if isinstance(e, APIStatusError):
        status = getattr(e, "status_code", getattr(e, "status", 0))
        status_code = int(status) if status else None
        if status == 429:
            return "rate_limit", status_code
        if status in (401, 403):
            return "auth", status_code
        if status == 529:
            return "overloaded", status_code
        if status >= 500:
            return "5xx_server", status_code
        if 400 <= status < 500:
            msg_lower = str(e).lower()
            if any(marker in msg_lower for marker in _CONTEXT_OVERFLOW_MARKERS) or (
                "message size" in msg_lower and "exceeds limit" in msg_lower
            ):
                return "context_overflow", status_code
            return "4xx_client", status_code
        return "other", status_code
    if isinstance(e, APIConnectionError):
        return "network", None
    if isinstance(e, (APITimeoutError, TimeoutError)):
        return "timeout", None
    if isinstance(e, APIEmptyResponseError):
        return "empty_response", None
    return "other", None


#: 4xx codes that are about *timing* rather than the request being wrong.
_RETRYABLE_4XX = frozenset({408, 409, 429})
#: 5xx codes that mean "I will never serve this", as opposed to "I am failing
#: right now". Everything else in the range is worth another attempt.
_PERMANENT_5XX = frozenset({501, 505})


def is_retryable_status_code(status_code: int) -> bool:
    """Whether an HTTP status from an LLM provider is worth another attempt.

    A whitelist of specific 5xx codes is the wrong shape for this: it has to
    guess in advance every way a server can be unwell, and it missed the
    520-527 family a provider behind Cloudflare returns when its origin is
    unreachable or the TLS handshake to it failed. A server erroring is
    generally a server erroring *now*, so the whole 5xx range is retried except
    the two codes that are a permanent refusal of the request itself.
    """
    if status_code in _RETRYABLE_4XX:
        return True
    return 500 <= status_code <= 599 and status_code not in _PERMANENT_5XX


#: Backoff between retries of a failed LLM call. The ceiling is well above the
#: sub-second waits this used to use: what is being waited out — a 429, a 502
#: from a backend mid-deploy, a 529 overload — clears on the order of seconds
#: to tens of seconds, and giving up after ~1s of total backoff turned a blip
#: the provider would have served on the next try into a dead turn.
_RETRY_WAIT_INITIAL_S = 0.5
_RETRY_WAIT_MAX_S = 30.0
#: Ceiling on a server-supplied ``Retry-After``. A provider asking for several
#: minutes is telling us to come back later, not to hang the turn that long.
_RETRY_AFTER_CAP_S = 60.0
_COMPACT_MAX_ATTEMPTS = 10
#: Compaction retries. A compact request that times out on one attempt may
#: succeed on a later one — the server load varies, and the model sometimes
#: finishes within the client's 600s timeout. We do NOT use `stop_never`
#: (what the web worker inherited from `max_retries_per_step == 0`): that
#: turned a genuinely too-big compact into a spin loop, with the UI stuck on
#: "Compacting..." because the retry counter only reached the Python log.
#: Ten attempts gives the model ~10 chances over several minutes; the
#: `CompactionRetry` wire event keeps the client informed between them.
#: `_compact_with_retry` raising after the cap is the intended exit path —
#: `compact_context` handles it by sending `CompactionEnd(success=False)`
#: and re-raising, so the UI always unsticks.

_exponential_backoff = wait_exponential_jitter(
    initial=_RETRY_WAIT_INITIAL_S, max=_RETRY_WAIT_MAX_S, jitter=0.5
)


def _server_requested_wait(error: BaseException | None) -> float | None:
    """The delay the server asked for, when it asked for a usable one."""
    retry_after = getattr(error, "retry_after", None)
    if isinstance(retry_after, bool) or not isinstance(retry_after, (int, float)):
        return None
    # Zero (or a date already past) is no guidance at all; taking it literally
    # would fire the remaining attempts back to back.
    return min(float(retry_after), _RETRY_AFTER_CAP_S) if retry_after > 0 else None


def _retry_wait(retry_state: RetryCallState) -> float:
    """How long to wait before the next attempt.

    A server that sent ``Retry-After`` knows better than any local schedule —
    on a rate limit it is the difference between a retry that succeeds and one
    that just spends another attempt earning the same 429. Everything else
    backs off exponentially.
    """
    error = retry_state.outcome.exception() if retry_state.outcome else None
    requested = _server_requested_wait(error)
    return requested if requested is not None else _exponential_backoff(retry_state)


def _is_request_too_large_error(e: Exception) -> bool:
    """Whether the error is a 400 "total message size ... exceeds limit" rejection.

    The provider enforces a hard request-body byte limit (2 MiB on Moonshot);
    this is recoverable by shrinking the request (compaction / image stripping).
    """
    if not isinstance(e, APIStatusError) or e.status_code != 400:
        return False
    msg_lower = str(e).lower()
    return "message size" in msg_lower and "exceeds limit" in msg_lower


def _is_context_overflow_error(e: Exception) -> bool:
    """Whether the provider refused the request for exceeding the context window.

    Distinct from ``_is_request_too_large_error``, which is about the request
    body's byte size. This one is about tokens: the local token estimate that
    drives auto-compaction is an approximation, so a turn can slip past the
    trigger ratio and still be refused. Compaction is the remedy either way,
    and the two are kept disjoint so each gets its own one-shot self-heal.
    """
    return classify_api_error(e)[0] == "context_overflow" and not _is_request_too_large_error(e)


def is_retryable_api_error(e: Exception) -> bool:
    """Classify retryability for the ``api_error`` telemetry event.

    Follows the shape of the TS ``isRetryableGenerateError``: transient status
    codes count as retryable, and any other ChatProviderError (network /
    timeout / empty-response) counts as retryable by the fallback rule.

    Deliberately broader than ``is_retryable_llm_error``, which decides what
    actually gets re-sent: a bare ``ChatProviderError`` is one nobody mapped —
    it covers local misconfiguration as much as a server hiccup — and reporting
    it as transient is not the same as retrying it blind. On status codes the
    two agree exactly, which they did not before: the loop used to skip
    408/409/529, so a 529 was reported as retryable and ended the turn anyway.
    """
    if isinstance(e, APIStatusError):
        return is_retryable_status_code(e.status_code)
    return isinstance(e, ChatProviderError)


def is_retryable_llm_error(e: BaseException) -> bool:
    """Whether an LLM call that failed this way is worth sending again.

    Drives the retry loop, and the ``kimo -p`` exit code that tells a supervisor
    whether re-running would help — the same question, so the same answer.

    Anything that failed in transit is worth another attempt: a dropped or reset
    connection, a timeout, a stream that ended with nothing in it. That includes
    the builtin ``TimeoutError`` and ``ConnectionError`` an asyncio or socket
    layer raises directly, which the provider-specific check did not recognize
    even though the error classifier already knew to call them transient.
    Status codes defer to ``is_retryable_status_code``.
    """
    if isinstance(e, (APIConnectionError, APITimeoutError, APIEmptyResponseError)):
        return True
    if isinstance(e, (TimeoutError, ConnectionError)):
        return True
    return isinstance(e, APIStatusError) and is_retryable_status_code(e.status_code)


def _provider_telemetry_kwargs(llm: LLM | None) -> dict[str, str]:
    if llm is None or llm.provider_config is None:
        return {}
    provider_type = llm.provider_config.type
    return {"provider_type": provider_type, "protocol": provider_type}


def _track_api_error(
    error: ChatProviderError,
    *,
    llm: LLM,
    duration_ms: int,
    input_tokens: int | None = None,
) -> None:
    from kimo_cli.telemetry import get_current_trace_id, track

    error_type, status_code = classify_api_error(error)
    properties: dict[str, Any] = {
        "error_type": error_type,
        "model": llm.model_name,
        "retryable": is_retryable_api_error(error),
        "duration_ms": duration_ms,
        **_provider_telemetry_kwargs(llm),
    }
    if status_code is not None:
        properties["status_code"] = status_code
    if input_tokens is not None and input_tokens > 0:
        properties["input_tokens"] = input_tokens
    trace_id = getattr(error, "trace_id", None) or get_current_trace_id()
    if trace_id:
        properties["trace_id"] = trace_id
    track("api_error", **properties)


type StepStopReason = Literal[
    "no_tool_calls", "tool_rejected", "tool_call_repeat", "max_steps_finalized"
]


@dataclass(frozen=True, slots=True)
class StepOutcome:
    stop_reason: StepStopReason
    assistant_message: Message


type TurnStopReason = StepStopReason


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    stop_reason: TurnStopReason
    final_message: Message | None
    step_count: int


def _explicit_only(checkpoints: Sequence[WikiCheckpoint]) -> tuple[WikiCheckpoint, ...]:
    """Keep only checkpoints the user explicitly asked for."""
    return tuple(c for c in checkpoints if c.cause == "explicit_user_durable")


class KimoSoul:
    """The soul of OpenKimo."""

    def __init__(
        self,
        agent: Agent,
        *,
        context: Context,
    ):
        """
        Initialize the soul.

        Args:
            agent (Agent): The agent to run.
            context (Context): The context of the agent.
        """
        self._agent = agent
        self._runtime = agent.runtime
        self._denwa_renji = agent.runtime.denwa_renji
        self._approval = agent.runtime.approval
        self._context = context
        self._loop_control = agent.runtime.config.loop_control
        self._compaction: Compaction = SimpleCompaction()  # TODO: maybe configurable and composable

        for tool in agent.toolset.tools:
            if tool.name == SendDMail_NAME:
                self._checkpoint_with_user_message = True
                break
        else:
            self._checkpoint_with_user_message = False

        self._steer_queue: asyncio.Queue[str | list[ContentPart]] = asyncio.Queue()
        self._last_tool_calls: list[tuple[str, str]] = []
        # Default to empty sets so code paths that bypass run() (tests, direct
        # _turn() drivers) never hit an AttributeError; run() resets both at
        # the start of every turn.
        self._issued_tool_calls: set[str] = set()
        self._completed_tool_calls: set[str] = set()
        # Snapshot the session's work dir (the user's workspace), not the
        # process cwd. For web-API workers the cwd is OpenKimo's own data
        # directory (Roaming\OpenKimo), where git snapshotting is both wrong
        # and can hang (huge .git); session.work_dir is the real workspace.
        self._fs_checkpointer = FsCheckpointer(
            workspace_from_env(Path(str(self._runtime.session.work_dir)))
        )
        self._truncation_continuations = 0
        self._loop_detector = LoopDetector()
        # Self-evolution evidence recorder. Recording is unconditional —
        # evidence collection is a default runtime behaviour, independent of
        # the self_evolve switch (see soul/evolution.py).
        self._evolution = EvolutionRecorder(self._runtime.session.dir)
        # Per-turn token counters, reset by run() and accumulated after each
        # LLM step, so the recorder can attribute usage to a single turn.
        self._turn_input_tokens = 0
        self._turn_output_tokens = 0
        self._turn_reasoning_tokens = 0
        # Cache for the tool-schema overhead estimate, keyed on tool names.
        # The tool set is stable within a turn; MCP late-load during turn
        # setup is the mutation path that invalidates it.
        self._tools_overhead_cache: tuple[int, int] | None = None
        self._tools_overhead_key: tuple[str, ...] | None = None
        self._tool_output_pruner: ToolOutputPruner | None = (
            ToolOutputPruner(
                protect_tokens=self._loop_control.prune_protect_tokens,
                minimum_tokens=self._loop_control.prune_minimum_tokens,
            )
            if self._loop_control.prune_tool_outputs
            else None
        )
        self._plan_mode: bool = self._runtime.session.state.plan_mode
        self._plan_session_id: str | None = self._runtime.session.state.plan_session_id
        self._root_trace_id: str | None = None
        # Pre-warm slug cache so the persisted slug survives process restarts
        if self._plan_session_id is not None and self._runtime.session.state.plan_slug is not None:
            from kimo_cli.tools.plan.heroes import seed_slug_cache

            seed_slug_cache(self._plan_session_id, self._runtime.session.state.plan_slug)
        self._pending_plan_activation_injection: bool = False
        if self._plan_mode:
            self._ensure_plan_session_id()
        self._injection_providers: list[DynamicInjectionProvider] = [
            PlanModeInjectionProvider(),
            TodoSyncInjectionProvider(),
            *(
                []
                if self._runtime.config.skip_afk_prompt_injection
                else [AfkModeInjectionProvider()]
            ),
            SessionMemoryInjectionProvider(),
            # Subagents share the parent's session and don't get their own
            # cross-session memory snapshot.
            *([CrossSessionMemoryInjectionProvider()] if self._runtime.role == "root" else []),
        ]
        self._hook_engine: HookEngine = HookEngine()
        self._stop_hook_active: bool = False
        if self.is_root:
            self._runtime.notifications.ack_ids("llm", extract_notification_ids(context.history))

        # Bind plan mode state to tools that support it
        self._bind_plan_mode_tools()

        self._slash_commands = self._build_slash_commands()
        self._slash_command_map = self._index_slash_commands(self._slash_commands)

    @property
    def name(self) -> str:
        return self._agent.name

    @property
    def model_name(self) -> str:
        return self._runtime.llm.chat_provider.model_name if self._runtime.llm else ""

    @property
    def model_capabilities(self) -> set[ModelCapability] | None:
        if self._runtime.llm is None:
            return None
        return self._runtime.llm.capabilities

    @property
    def is_yolo(self) -> bool:
        """Whether explicit yolo mode is active."""
        return self._approval.is_yolo()

    @property
    def is_auto_approve(self) -> bool:
        """Whether tool approvals are bypassed (explicit yolo, or implied by afk)."""
        return self._approval.is_auto_approve()

    @property
    def is_afk(self) -> bool:
        """Whether no user is present (away-from-keyboard)."""
        return self._approval.is_afk()

    @property
    def is_afk_flag(self) -> bool:
        """Whether persisted afk mode is active."""
        return self._approval.is_afk_flag()

    @property
    def is_root(self) -> bool:
        """Whether this soul is the root session rather than a subagent."""
        return self._runtime.role == "root"

    @property
    def todos(self) -> list[TodoItemState]:
        """The persisted root todo list. Subagent todos live in their own
        state files, so for a subagent this intentionally returns the shared
        session state — callers must gate on ``is_subagent`` first."""
        try:
            return list(self._runtime.session.state.todos)
        except Exception:
            return []

    @property
    def is_subagent(self) -> bool:
        """Whether this soul is running as a subagent rather than the root session."""
        return self._runtime.role == "subagent"

    @property
    def root_trace_id(self) -> str | None:
        """The latest LLM trace id for this root session, for UI events."""
        return self._root_trace_id if self.is_root else None

    def _set_trace_id(self, trace_id: str | None) -> None:
        from kimo_cli.telemetry import set_current_trace_id

        set_current_trace_id(trace_id)
        if self.is_root:
            self._root_trace_id = trace_id

    @property
    def plan_mode(self) -> bool:
        """Whether plan mode (read-only research and planning) is active."""
        return self._plan_mode

    @property
    def hook_engine(self) -> HookEngine:
        return self._hook_engine

    def set_hook_engine(self, engine: HookEngine) -> None:
        self._hook_engine = engine
        if isinstance(self._agent.toolset, KimoToolset):
            self._agent.toolset.set_hook_engine(engine)

    def add_injection_provider(self, provider: DynamicInjectionProvider) -> None:
        """Register an additional dynamic injection provider."""
        self._injection_providers.append(provider)

    async def _collect_injections(self) -> list[DynamicInjection]:
        """Collect dynamic injections from all registered providers."""
        injections: list[DynamicInjection] = []
        for provider in self._injection_providers:
            try:
                result = await provider.get_injections(self._context.history, self)
                injections.extend(result)
            except Exception:
                logger.warning(
                    "injection provider %s failed",
                    type(provider).__name__,
                    exc_info=True,
                )
        return injections

    async def _notify_injection_providers_compacted(self) -> None:
        """Notify all injection providers that the context has been compacted.

        Failures are isolated per-provider so a buggy third-party provider
        cannot abort compaction (which would skip CompactionEnd wire events
        and PostCompact telemetry).
        """
        for provider in self._injection_providers:
            try:
                await provider.on_context_compacted()
            except Exception:
                logger.warning(
                    "injection provider %s on_context_compacted failed",
                    type(provider).__name__,
                    exc_info=True,
                )

    async def notify_afk_changed(self, enabled: bool) -> None:
        """Notify dynamic injection providers that afk mode changed."""
        for provider in self._injection_providers:
            try:
                await provider.on_afk_changed(enabled)
            except Exception:
                logger.warning(
                    "injection provider %s on_afk_changed failed",
                    type(provider).__name__,
                    exc_info=True,
                )

    def _bind_plan_mode_tools(self) -> None:
        """Bind plan mode state to tools that support it."""
        if not isinstance(self._agent.toolset, KimoToolset):
            return

        def checker() -> bool:
            return self._plan_mode

        def path_getter() -> Path | None:
            return self.get_plan_file_path()

        # WriteFile gets both checker and path_getter (for plan file auto-approve)
        from kimo_cli.tools.file.write import WriteFile

        write_tool = self._agent.toolset.find("WriteFile")
        if isinstance(write_tool, WriteFile):
            write_tool.bind_plan_mode(checker, path_getter)

        from kimo_cli.tools.file.replace import StrReplaceFile

        replace_tool = self._agent.toolset.find("StrReplaceFile")
        if isinstance(replace_tool, StrReplaceFile):
            replace_tool.bind_plan_mode(checker, path_getter)

        # ExitPlanMode has a special bind() method
        from kimo_cli.tools.plan import ExitPlanMode

        exit_tool = self._agent.toolset.find("ExitPlanMode")
        if isinstance(exit_tool, ExitPlanMode):
            exit_tool.bind(
                self.toggle_plan_mode,
                path_getter,
                checker,
                self._approval.is_afk,
            )

        # EnterPlanMode has a special bind() method
        from kimo_cli.tools.plan.enter import EnterPlanMode

        enter_tool = self._agent.toolset.find("EnterPlanMode")
        if isinstance(enter_tool, EnterPlanMode):
            enter_tool.bind(
                self.toggle_plan_mode,
                path_getter,
                checker,
                self._approval.is_auto_approve,
            )

        # AskUserQuestion — bind afk checker for auto-dismiss.
        # Yolo alone keeps the tool live; only afk (no user present) dismisses.
        from kimo_cli.tools.ask_user import AskUserQuestion

        ask_tool = self._agent.toolset.find("AskUserQuestion")
        if isinstance(ask_tool, AskUserQuestion):
            ask_tool.bind_afk(self._approval.is_afk)

    def rebind_plan_mode_tools(self) -> None:
        """Take the plan-mode bindings back from a soul that shares our tools.

        A second soul built on the same agent shares the tool objects, so its
        constructor rebinds them to itself — the plan-mode state, the plan file
        path and the plan-mode callbacks. The owner calls this afterwards to
        restore its own bindings.
        """
        self._bind_plan_mode_tools()

    def _ensure_plan_session_id(self) -> None:
        """Allocate a stable plan session ID on first activation."""
        if self._plan_session_id is None:
            import uuid

            self._plan_session_id = uuid.uuid4().hex
            self._runtime.session.state.plan_session_id = self._plan_session_id
            # Compute and persist slug immediately so the path survives process restarts
            from kimo_cli.tools.plan.heroes import get_or_create_slug

            slug = get_or_create_slug(self._plan_session_id)
            self._runtime.session.state.plan_slug = slug
            self._runtime.session.save_state()

    def _set_plan_mode(self, enabled: bool, *, source: Literal["manual", "tool"]) -> bool:
        """Update plan mode state for either manual or tool-driven toggles."""
        if enabled == self._plan_mode:
            return self._plan_mode
        self._plan_mode = enabled
        if enabled:
            self._ensure_plan_session_id()
            self._pending_plan_activation_injection = source == "manual"
        else:
            self._pending_plan_activation_injection = False
            self._plan_session_id = None
            self._runtime.session.state.plan_session_id = None
            self._runtime.session.state.plan_slug = None
        # Persist plan mode to session state so it survives process restarts
        self._runtime.session.state.plan_mode = self._plan_mode
        self._runtime.session.save_state()
        return self._plan_mode

    def get_plan_file_path(self) -> Path | None:
        """Get the plan file path for the current session."""
        if self._plan_session_id is None:
            return None
        from kimo_cli.tools.plan.heroes import get_plan_file_path

        return get_plan_file_path(self._plan_session_id)

    def read_current_plan(self) -> str | None:
        """Read the current plan file content."""
        if self._plan_session_id is None:
            return None
        from kimo_cli.tools.plan.heroes import read_plan_file

        return read_plan_file(self._plan_session_id)

    def clear_current_plan(self) -> None:
        """Delete the current plan file."""
        path = self.get_plan_file_path()
        if path and path.exists():
            path.unlink()

    async def toggle_plan_mode(self) -> bool:
        """Toggle plan mode on/off. Returns the new state.

        Tools are not hidden/unhidden — instead, each tool checks plan mode
        state at call time and rejects if blocked.
        Periodic reminders are handled by the dynamic injection system.
        """
        return self._set_plan_mode(not self._plan_mode, source="tool")

    async def toggle_plan_mode_from_manual(self) -> bool:
        """Toggle plan mode from UI/manual entry points (slash command, keybinding)."""
        return self._set_plan_mode(not self._plan_mode, source="manual")

    async def set_plan_mode_from_manual(self, enabled: bool) -> bool:
        """Set plan mode to a specific state from UI/manual entry points.

        Unlike toggle, this accepts the desired state directly, avoiding
        race conditions when the caller already knows the target value.
        """
        return self._set_plan_mode(enabled, source="manual")

    def schedule_plan_activation_reminder(self) -> None:
        """Schedule a plan-mode activation reminder for the next turn.

        Use this when plan mode is already active (e.g. restored session with
        ``--plan`` flag) and ``_set_plan_mode`` would early-return because the
        state hasn't actually changed.
        """
        if self._plan_mode:
            self._pending_plan_activation_injection = True

    def consume_pending_plan_activation_injection(self) -> bool:
        """Consume the next-step activation reminder scheduled by a manual toggle."""
        if not self._plan_mode or not self._pending_plan_activation_injection:
            return False
        self._pending_plan_activation_injection = False
        return True

    @property
    def thinking(self) -> bool | None:
        """Whether thinking mode is enabled."""
        if self._runtime.llm is None:
            return None
        if thinking_effort := self._runtime.llm.chat_provider.thinking_effort:
            return thinking_effort != "off"
        return None

    @property
    def status(self) -> StatusSnapshot:
        token_count = self._context.token_count
        max_size = self._runtime.llm.max_context_size if self._runtime.llm is not None else 0
        return StatusSnapshot(
            context_usage=self._context_usage,
            yolo_enabled=self._approval.is_yolo_flag(),
            afk_enabled=self._approval.is_afk(),
            plan_mode=self._plan_mode,
            context_tokens=token_count,
            max_context_tokens=max_size,
            mcp_status=self._mcp_status_snapshot(),
        )

    @property
    def agent(self) -> Agent:
        return self._agent

    @property
    def runtime(self) -> Runtime:
        return self._runtime

    @property
    def context(self) -> Context:
        return self._context

    @property
    def _context_usage(self) -> float:
        if self._runtime.llm is not None and self._runtime.llm.max_context_size > 0:
            return self._context.token_count / self._runtime.llm.max_context_size
        return 0.0

    @property
    def wire_file(self) -> WireFile:
        return self._runtime.session.wire_file

    def _mcp_status_snapshot(self):
        if not isinstance(self._agent.toolset, KimoToolset):
            return None
        return self._agent.toolset.mcp_status_snapshot()

    async def start_background_mcp_loading(self) -> bool:
        """Start deferred MCP loading, if any, without exposing toolset internals."""
        if not isinstance(self._agent.toolset, KimoToolset):
            return False
        return await self._agent.toolset.start_deferred_mcp_tool_loading()

    async def wait_for_background_mcp_loading(self) -> None:
        """Wait for any in-flight MCP startup to finish."""
        if not isinstance(self._agent.toolset, KimoToolset):
            return
        await self._agent.toolset.wait_for_mcp_tools()

    async def _checkpoint(self):
        checkpoint_id = await self._context.checkpoint(self._checkpoint_with_user_message)
        # Snapshot the workspace too, so a D-Mail revert can restore files
        # and not just the conversation (denwarenji TODO). Fail-open: the
        # checkpointer disables itself when git is missing or the workspace
        # is a user-owned repo.
        await self._fs_checkpointer.snapshot(checkpoint_id)

    def steer(self, content: str | list[ContentPart]) -> None:
        """Queue a steer message for injection into the current turn."""
        self._steer_queue.put_nowait(content)

    async def _consume_pending_steers(self) -> bool:
        """Drain the steer queue and inject as follow-up user messages.

        Returns True if any steers were consumed.

        Note: /btw is intercepted at the UI layer (``classify_input``) before
        reaching the steer queue, so it never appears here.
        """
        consumed = False
        while not self._steer_queue.empty():
            content = self._steer_queue.get_nowait()
            await self._inject_steer(content)
            wire_send(SteerInput(user_input=content))
            consumed = True
        return consumed

    async def _inject_steer(self, content: str | list[ContentPart]) -> None:
        """Inject a single steer as a regular follow-up user message."""
        parts = cast(
            list[ContentPart],
            [TextPart(text=content)] if isinstance(content, str) else list(content),
        )
        message = Message(role="user", content=parts)
        if self._runtime.llm is None:
            raise LLMNotSet()
        if missing_caps := check_message(message, self._runtime.llm.capabilities):
            raise LLMNotSupported(self._runtime.llm, list(missing_caps))
        from kimo_cli.tools.wiki import extend_wiki_turn_context
        from kimo_cli.wiki.trusted_paths import trust_user_mentioned_paths

        steer_text = message.extract_text(" ")
        extend_wiki_turn_context(steer_text)
        await asyncio.to_thread(trust_user_mentioned_paths, self._runtime, steer_text)
        await self._context.append_message(message)

    @property
    def available_slash_commands(self) -> list[SlashCommand[Any]]:
        return self._slash_commands

    async def run(
        self,
        user_input: str | list[ContentPart],
        *,
        skip_user_prompt_hook: bool = False,
    ):
        approval_source_token = None
        created_approval_source: ApprovalSource | None = None
        turn_started = False
        turn_finished = False
        interrupt_reason: str | None = None
        turn_t0 = time.monotonic()
        # Defined before try so the finally block can read it even if the
        # turn died before user_message was built.
        text_input = ""
        # Reset per-turn token counters for the self-evolution recorder.
        self._turn_input_tokens = 0
        self._turn_output_tokens = 0
        self._turn_reasoning_tokens = 0
        wiki_turn_token = None
        wiki_root_turn_id: str | None = None
        # Track tool_call ids that flowed to the wire as ToolCall vs as
        # ToolResult, so the finally block below can emit a compensating
        # ToolResult for any that were dispatched but never completed (e.g.
        # a foreground Agent tool that was still running when the user hit
        # Stop). Without this, the wire carries a ToolCall with no matching
        # ToolResult, and consumers that key off "terminal per tool_call_id"
        # (the web UI's `subagentRunning`) hang in an "Agent working…"
        # state for the rest of the session, even after a resume.
        #
        # Stored on `self` (not as locals) because `_run_step_once` runs
        # inside `_step` — a separate class method whose closures don't
        # reach run()'s locals.
        self._issued_tool_calls: set[str] = set()
        self._completed_tool_calls: set[str] = set()
        self._set_trace_id(None)
        if get_current_approval_source_or_none() is None:
            created_approval_source = ApprovalSource(kind="foreground_turn", id=uuid.uuid4().hex)
            approval_source_token = set_current_approval_source(created_approval_source)
        try:
            await self._refresh_managed_skills()
            # Refresh OAuth tokens on each turn to avoid idle-time expirations.
            await self._runtime.oauth.ensure_fresh(self._runtime)

            # Set session_id ContextVar for toolset hooks
            from kimo_cli.soul.toolset import set_session_id

            set_session_id(self._runtime.session.id)

            from kimo_cli.hooks import events

            # --- UserPromptSubmit hook ---
            # Synthetic internal prompts (e.g. background-task notification
            # follow-ups injected by ``Print`` after a bg task finishes or
            # the wait ceiling is hit) must bypass ``UserPromptSubmit``:
            # they are not user input, and a user-configured prompt-blocking
            # hook would drop the notification and hang the wait loop.
            if not skip_user_prompt_hook:
                text_input_for_hook = user_input if isinstance(user_input, str) else ""

                hook_results = await self._hook_engine.trigger(
                    "UserPromptSubmit",
                    matcher_value=text_input_for_hook,
                    input_data=events.user_prompt_submit(
                        session_id=self._runtime.session.id,
                        cwd=str(Path.cwd()),
                        prompt=text_input_for_hook,
                    ),
                )
                for result in hook_results:
                    if result.action == "block":
                        wire_send(TurnBegin(user_input=user_input))
                        turn_started = True
                        wire_send(TextPart(text=result.reason or "Prompt blocked by hook."))
                        wire_send(TurnEnd())
                        turn_finished = True
                        return

            wire_send(TurnBegin(user_input=user_input, todos=self._todos_snapshot()))
            turn_started = True
            from kimo_cli.telemetry import track as _track_telemetry

            _track_telemetry(
                "turn_started",
                mode="plan" if self._plan_mode else "agent",
                **_provider_telemetry_kwargs(self._runtime.llm),
            )
            user_message = Message(role="user", content=user_input)
            raw_text_input = user_message.extract_text(" ")
            text_input = raw_text_input.strip()
            from kimo_cli.tools.wiki import set_wiki_turn_context

            wiki_turn_token = set_wiki_turn_context(
                self._runtime,
                raw_text_input,
                trusted_user_input=not skip_user_prompt_hook,
            )
            if not skip_user_prompt_hook:
                from kimo_cli.wiki.trusted_paths import trust_user_mentioned_paths

                await asyncio.to_thread(trust_user_mentioned_paths, self._runtime, raw_text_input)

            command_call = parse_slash_command_call(text_input)
            if (
                command_call is None
                and not skip_user_prompt_hook
                and self.is_root
                and self._runtime.wiki is not None
                and self._runtime.wiki_coordinator is not None
            ):
                from kimo_cli.wiki.intent import detect_durable_intent

                coordinator = self._runtime.wiki_coordinator
                try:
                    root_turn = await coordinator.begin_turn(raw_text_input, text_input)
                    wiki_root_turn_id = root_turn.root_turn_id
                    if self._runtime.wiki_evidence_reporter is not None:
                        self._runtime.wiki_evidence_reporter.start_root_turn(root_turn.root_turn_id)
                    # Explicit intent is opened before retrieval and before the
                    # first model step, so it cannot be shaped by either.
                    intent = detect_durable_intent(raw_text_input)
                    if intent is not None:
                        await coordinator.record_durable_intent(intent)
                    # No automatic per-turn search. The Wiki index already sits
                    # in the system prompt, so the agent can see what exists and
                    # call Wiki(operation="search") when a turn actually calls
                    # for it. Searching every prompt regardless of relevance cost
                    # latency and prompt space on conversations that never
                    # needed it.
                except Exception:
                    logger.warning("Wiki turn setup failed; continuing without it")

            if command_call:
                command = self._find_slash_command(command_call.name)
                if command is None:
                    # this should not happen actually, the shell should have filtered it out
                    wire_send(TextPart(text=f'Unknown slash command "/{command_call.name}".'))
                else:
                    ret = command.func(self, command_call.args)
                    if isinstance(ret, Awaitable):
                        await ret
            elif self._loop_control.max_ralph_iterations != 0:
                runner = FlowRunner.ralph_loop(
                    user_message,
                    self._loop_control.max_ralph_iterations,
                )
                await runner.run(self, "")
            else:
                await self._turn(user_message)

            # --- Stop hook (max 1 re-trigger to prevent infinite loop) ---
            if not self._stop_hook_active:
                stop_results = await self._hook_engine.trigger(
                    "Stop",
                    input_data=events.stop(
                        session_id=self._runtime.session.id,
                        cwd=str(Path.cwd()),
                        stop_hook_active=False,
                    ),
                )
                for result in stop_results:
                    if result.action == "block" and result.reason:
                        self._stop_hook_active = True
                        try:
                            await self._turn(Message(role="user", content=result.reason))
                        finally:
                            self._stop_hook_active = False
                        break

            wire_send(TurnEnd())
            turn_finished = True

            # Auto-set title after first real turn (skip slash commands), from
            # the first thing the person asked rather than from this turn: an
            # earlier turn that was stopped still names the session. Only the
            # root does it: a subagent shares the session, and its prompt is
            # the agent's, not the person's.
            if not command_call and self.is_root:
                session = self._runtime.session
                if session.state.custom_title is None:
                    from kimo_cli.utils.string import shorten

                    title = shorten(
                        await session.first_prompt()
                        or Message(role="user", content=user_input).extract_text(" "),
                        width=50,
                    )
                    if title:
                        from kimo_cli.session_state import (
                            load_session_state,
                            save_session_state,
                        )

                        # Read-modify-write: load fresh state to avoid
                        # overwriting concurrent web changes
                        fresh = load_session_state(session.dir)
                        if fresh.custom_title is None:
                            fresh.custom_title = title
                            save_session_state(fresh, session.dir)
                        session.state.custom_title = fresh.custom_title
        except MaxStepsReached:
            interrupt_reason = "max_steps"
            raise
        except asyncio.CancelledError:
            interrupt_reason = "user_cancelled"
            raise
        except Exception:
            interrupt_reason = "error"
            raise
        finally:
            from kimo_cli.telemetry import get_current_trace_id

            # Compensating ToolResults for any tool_call_id that made it onto
            # the wire as a ToolCall but never returned a ToolResult. This
            # covers user-cancel and error paths: without it, the wire
            # carries an orphaned ToolCall that the web UI (and any other
            # consumer that keys terminal state off tool_call_id) reads as
            # "still running" for the rest of the session — even after a
            # resume. Normal completion is a no-op: the diff is empty.
            #
            # Only outstanding ids, never re-emitted ones, so a background
            # Agent tool's already-sent dispatch ToolResult is not double-
            # compensated. Sort for deterministic wire order under cancel.
            outstanding_tool_calls = self._issued_tool_calls - self._completed_tool_calls
            if outstanding_tool_calls:
                logger.warning(
                    "Emitting compensating ToolResult for {count} tool_call(s) "
                    "with no result before turn ended (reason={reason})",
                    count=len(outstanding_tool_calls),
                    reason=interrupt_reason or "error",
                )
                for tc_id in sorted(outstanding_tool_calls):
                    wire_send(
                        ToolResult(
                            tool_call_id=tc_id,
                            return_value=ToolError(
                                message="Tool call was cancelled before returning a result.",
                                brief="Cancelled",
                            ),
                        )
                    )

            if turn_started and not turn_finished:
                wire_send(TurnEnd(interrupt_reason=interrupt_reason or "error"))
                from kimo_cli.telemetry import track as _track_telemetry

                _interrupt_kwargs: dict[str, Any] = dict(
                    mode="plan" if self._plan_mode else "agent",
                    at_step=getattr(self, "_current_step_no", 0),
                    interrupt_reason=interrupt_reason or "error",
                    **_provider_telemetry_kwargs(self._runtime.llm),
                )
                if _tid := get_current_trace_id():
                    _interrupt_kwargs["trace_id"] = _tid
                _track_telemetry("turn_interrupted", **_interrupt_kwargs)
            if turn_started:
                # turn_ended fires unconditionally once a turn began (TS parity).
                from kimo_cli.telemetry import track as _track_ended

                _ended_kwargs: dict[str, Any] = dict(
                    reason=(
                        "completed"
                        if turn_finished
                        else "cancelled"
                        if interrupt_reason == "user_cancelled"
                        else "failed"
                    ),
                    duration_ms=int((time.monotonic() - turn_t0) * 1000),
                    mode="plan" if self._plan_mode else "agent",
                )
                _ended_kwargs.update(_provider_telemetry_kwargs(self._runtime.llm))
                if _tid := get_current_trace_id():
                    _ended_kwargs["trace_id"] = _tid
                _track_ended("turn_ended", **_ended_kwargs)
            if turn_started:
                # Self-evolution: record this turn's execution evidence.
                # Unconditional — evidence collection is default behaviour,
                # independent of the self_evolve switch. The recorder never
                # raises (see soul/evolution.py), so recording cannot break
                # the turn even from the finally block.
                # task: the turn's prompt, truncated; slash-command turns
                # and empty prompts record None (nothing replayable).
                task_text = text_input[:300] or None
                if task_text is not None and parse_slash_command_call(task_text) is not None:
                    task_text = None
                try:
                    self._evolution.record_turn(
                        EvolutionTurnRecord(
                            ts=time.time(),
                            mode="plan" if self._plan_mode else "agent",
                            outcome=(
                                "completed"
                                if turn_finished
                                else "cancelled"
                                if interrupt_reason == "user_cancelled"
                                else "failed"
                            ),
                            steps=getattr(self, "_current_step_no", 0),
                            duration_ms=int((time.monotonic() - turn_t0) * 1000),
                            input_tokens=self._turn_input_tokens,
                            output_tokens=self._turn_output_tokens,
                            reasoning_tokens=self._turn_reasoning_tokens,
                            task=task_text,
                        )
                    )
                except Exception:
                    logger.warning("Failed to record evolution turn evidence", exc_info=True)
                # When the self-evolution switch is on, dispatch an
                # auto-evolve cycle as a fire-and-forget background task.
                # Evidence collection above is unconditional; this step is
                # gated inside auto_evolve_maybe by config.default_self_evolve
                # and by a recursion guard against verify subprocesses.
                # Nothing here blocks the turn — the task runs on the same
                # event loop after this finally block completes.
                try:
                    from kimo_cli.evolution.auto_evolve import schedule_auto_evolve

                    schedule_auto_evolve(self._runtime.config, self._runtime.session.work_dir)
                except Exception:
                    logger.warning("Failed to dispatch auto-evolve", exc_info=True)
            if created_approval_source is not None and self._runtime.approval_runtime is not None:
                self._runtime.approval_runtime.cancel_by_source(
                    created_approval_source.kind,
                    created_approval_source.id,
                )
            if approval_source_token is not None:
                reset_current_approval_source(approval_source_token)
            if wiki_turn_token is not None:
                from kimo_cli.tools.wiki import reset_wiki_turn_context

                reset_wiki_turn_context(wiki_turn_token)
            if wiki_root_turn_id is not None:
                coordinator = self._runtime.wiki_coordinator
                if coordinator is not None:
                    try:
                        # Every real turn is retired here, on any stop reason.
                        # The resolution loop only runs on a no-tool completion,
                        # so a rejected or step-capped turn would otherwise leave
                        # its checkpoints pending until they wedge the coordinator.
                        if turn_finished:
                            await coordinator.finish_turn(wiki_root_turn_id)
                        else:
                            await coordinator.cancel_turn(wiki_root_turn_id)
                    except Exception:
                        logger.warning("Wiki turn retirement failed")
            if wiki_root_turn_id is not None and self._runtime.wiki_evidence_reporter is not None:
                self._runtime.wiki_evidence_reporter.finish_root_turn(wiki_root_turn_id)
            self._set_trace_id(None)

    async def _refresh_managed_skills(self) -> None:
        """Refresh the merged catalog before a turn when managed state changed."""
        from kimo_cli.skill import (
            discover_skills_from_roots,
            format_skills_for_prompt,
            index_skills,
            resolve_skills_roots,
        )
        from kimo_cli.skill.manager import SkillManager
        from kimo_cli.utils.path import is_within_directory

        revision = SkillManager().revision
        if revision == self._runtime.managed_skill_revision:
            return
        roots = await resolve_skills_roots(
            self._runtime.session.work_dir,
            skills_dirs=self._runtime.requested_skills_dirs,
            merge_brands=self._runtime.config.merge_all_available_skills,
            brands=self._runtime.config.skill_brands,
            extra_skill_dirs=self._runtime.config.extra_skill_dirs or None,
            skip_skill_dirs=self._runtime.config.skip_skill_dirs or None,
        )
        skills = await discover_skills_from_roots(roots)
        new_prompt = format_skills_for_prompt(skills) or "No skills found."
        old_prompt = self._runtime.skills_prompt
        if old_prompt in self._agent.system_prompt:
            object.__setattr__(
                self._agent,
                "system_prompt",
                self._agent.system_prompt.replace(old_prompt, new_prompt, 1),
            )
        self._runtime.skills = index_skills(skills)
        self._runtime.skills_dirs[:] = [
            root.root.canonical()
            for root in roots
            if not is_within_directory(root.root, self._runtime.session.work_dir)
        ]
        self._runtime.skills_prompt = new_prompt
        self._slash_commands = self._build_slash_commands()
        self._slash_command_map = self._index_slash_commands(self._slash_commands)
        self._runtime.managed_skill_revision = revision

    async def _turn(self, user_message: Message) -> TurnOutcome:
        if self._runtime.llm is None:
            raise LLMNotSet()

        if missing_caps := check_message(user_message, self._runtime.llm.capabilities):
            raise LLMNotSupported(self._runtime.llm, list(missing_caps))

        self._last_tool_calls = []
        self._truncation_continuations = 0
        self._loop_detector.reset()
        await self._checkpoint()  # this creates the checkpoint 0 on first run
        await self._pre_sampling_compact(user_message)
        await self._context.append_message(user_message)
        logger.debug("Appended user message to context")
        opening = await self._explicit_checkpoint_message()
        if opening is not None:
            await self._context.append_message(opening)
        return await self._agent_loop()

    def _build_slash_commands(self) -> list[SlashCommand[Any]]:
        commands: list[SlashCommand[Any]] = list(soul_slash_registry.list_commands())
        seen_names = {cmd.name for cmd in commands}

        for skill in self._runtime.skills.values():
            if skill.type not in ("standard", "flow"):
                continue
            name = f"{SKILL_COMMAND_PREFIX}{skill.name}"
            if name in seen_names:
                logger.warning(
                    "Skipping skill slash command /{name}: name already registered",
                    name=name,
                )
                continue
            commands.append(
                SlashCommand(
                    name=name,
                    func=self._make_skill_runner(skill),
                    description=skill.description or "",
                    aliases=[],
                )
            )
            seen_names.add(name)

        for skill in self._runtime.skills.values():
            if skill.type != "flow":
                continue
            if skill.flow is None:
                logger.warning("Flow skill {name} has no flow; skipping", name=skill.name)
                continue
            command_name = f"{FLOW_COMMAND_PREFIX}{skill.name}"
            if command_name in seen_names:
                logger.warning(
                    "Skipping prompt flow slash command /{name}: name already registered",
                    name=command_name,
                )
                continue
            runner = FlowRunner(skill.flow, name=skill.name)
            commands.append(
                SlashCommand(
                    name=command_name,
                    func=runner.run,
                    description=skill.description or "",
                    aliases=[],
                )
            )
            seen_names.add(command_name)

        return commands

    @staticmethod
    def _index_slash_commands(
        commands: list[SlashCommand[Any]],
    ) -> dict[str, SlashCommand[Any]]:
        indexed: dict[str, SlashCommand[Any]] = {}
        for command in commands:
            indexed[command.name] = command
            for alias in command.aliases:
                indexed[alias] = command
        return indexed

    def _find_slash_command(self, name: str) -> SlashCommand[Any] | None:
        return self._slash_command_map.get(name)

    def _make_skill_runner(self, skill: Skill) -> Callable[[KimoSoul, str], None | Awaitable[None]]:
        async def _run_skill(soul: KimoSoul, args: str, *, _skill: Skill = skill) -> None:
            from kimo_cli.telemetry import track

            track("skill_invoked", skill_name=_skill.name)
            skill_text = await read_skill_text(_skill)
            if skill_text is None:
                wire_send(
                    TextPart(text=f'Failed to load skill "/{SKILL_COMMAND_PREFIX}{_skill.name}".')
                )
                return
            extra = args.strip()
            if extra:
                skill_text = f"{skill_text}\n\nUser request:\n{extra}"
            await soul._turn(Message(role="user", content=skill_text))

        _run_skill.__doc__ = skill.description
        return _run_skill

    async def _agent_loop(self) -> TurnOutcome:
        """The main agent loop for one run.

        Lifecycle:
            1. Turn Initialization   - clean up stale steers, load MCP tools.
            2. Step Loop             - iterate until the turn stops or fails.
               a. Step Guard         - enforce max-steps-per-turn limit.
               b. Step Begin         - emit StepBegin wire event.
               c. Context Compaction - auto-compact if context exceeds trigger ratio.
               d. Checkpoint         - persist current state before calling LLM.
               e. Step Execution     - run _step() (LLM call + tool execution).
               f. Error Handling     - BackToTheFuture (revert) or fatal exception.
               g. Outcome Resolution - steers / stop / continue.
            3. Turn Resolution       - return TurnOutcome to the caller.
        """
        assert self._runtime.llm is not None

        # ═══════════════════════════════════════════════════════════════════════
        # 1. TURN INITIALIZATION
        # ═══════════════════════════════════════════════════════════════════════

        # Discard any stale steers from a previous turn.
        while not self._steer_queue.empty():
            self._steer_queue.get_nowait()

        # ── 1a. MCP deferred loading ──────────────────────────────────────────
        if isinstance(self._agent.toolset, KimoToolset):
            await self.start_background_mcp_loading()
            loading = bool((snapshot := self._mcp_status_snapshot()) and snapshot.loading)
            if loading:
                wire_send(StatusUpdate(mcp_status=snapshot))
                wire_send(MCPLoadingBegin())
            try:
                await self.wait_for_background_mcp_loading()
                # Track MCP connection result
                if loading:
                    from kimo_cli.telemetry import track as _track_mcp

                    mcp_snap = self._mcp_status_snapshot()
                    if mcp_snap:
                        if mcp_snap.connected > 0:
                            _track_mcp(
                                "mcp_connected",
                                server_count=mcp_snap.connected,
                                total_count=mcp_snap.total,
                            )
                        _failed = mcp_snap.total - mcp_snap.connected
                        if _failed > 0:
                            _track_mcp(
                                "mcp_failed",
                                failed_count=_failed,
                                total_count=mcp_snap.total,
                            )
            finally:
                if loading:
                    wire_send(StatusUpdate(mcp_status=self._mcp_status_snapshot()))
                    wire_send(MCPLoadingEnd())

        # ═══════════════════════════════════════════════════════════════════════
        # 2. STEP LOOP
        # ═══════════════════════════════════════════════════════════════════════
        step_no = 0
        self._current_step_no = 0
        oversized_request_retried = False
        context_overflow_retried = False
        while True:
            step_no += 1

            # ── 2a. Step Guard ──────────────────────────────────────────────────
            if step_no > self._loop_control.max_steps_per_turn:
                # 2a-0. Finalization round: the step budget is exhausted, but
                # ending the turn on an exception leaves the user with no
                # answer at all. One no-tools round lets the model wrap up
                # (completed / unfinished / final answer). Any failure falls
                # back to the historical raise — strictly additive behavior.
                if self._loop_control.finalize_on_max_steps:
                    finalized = await self._finalize_after_max_steps(step_no)
                    if finalized is not None:
                        return TurnOutcome(
                            stop_reason="max_steps_finalized",
                            final_message=finalized,
                            step_count=step_no - 1,
                        )
                raise MaxStepsReached(self._loop_control.max_steps_per_turn)

            self._current_step_no = step_no

            # ── 2b. Step Begin ──────────────────────────────────────────────────
            wire_send(StepBegin(n=step_no))
            back_to_the_future: BackToTheFuture | None = None
            step_outcome: StepOutcome | None = None

            try:
                # ── 2c. Context Compaction ──────────────────────────────────────
                # The trigger counts tool schemas too (message history alone
                # undercounts the request the provider actually sees).
                tools_tokens, tools_bytes = self._tools_overhead()
                if should_auto_compact(
                    self._context.token_count_with_pending + tools_tokens,
                    self._runtime.llm.max_context_size,
                    trigger_ratio=self._loop_control.compaction_trigger_ratio,
                    reserved_context_size=self._loop_control.reserved_context_size,
                    request_bytes=self._estimated_request_bytes() + tools_bytes,
                    max_request_bytes=self._loop_control.max_request_bytes,
                ):
                    logger.info("Context too long, compacting...")
                    try:
                        await self.compact_context()
                    except Exception as compact_err:
                        # Compaction is upkeep; its failure does not end the
                        # turn. The request may still fit, and if it does not,
                        # the provider says so and the overflow handling below
                        # (compact, then retry the step) takes over.
                        logger.error(
                            "Context compaction failed at step {step_no}: {error_type}: {error}; "
                            "continuing without it",
                            step_no=step_no,
                            error_type=type(compact_err).__name__,
                            error=compact_err,
                        )

                # ── 2d. Checkpoint ──────────────────────────────────────────────
                logger.debug("Beginning step {step_no}", step_no=step_no)
                await self._checkpoint()
                self._denwa_renji.set_n_checkpoints(self._context.n_checkpoints)

                # ── 2e. Step Execution ──────────────────────────────────────────
                step_outcome = await self._step()

            except BackToTheFuture as e:
                # ── 2f-i. D-Mail revert signal ────────────────────────────────
                back_to_the_future = e

            except Exception as e:
                # ── 2f-0. Oversized request self-heal (400 message-size limit) ──
                if not oversized_request_retried and _is_request_too_large_error(e):
                    oversized_request_retried = True
                    logger.warning(
                        "Request body exceeded the provider size limit at step {step_no}; "
                        "attempting self-heal (compaction / image stripping)",
                        step_no=step_no,
                    )
                    if await self._self_heal_oversized_request():
                        continue

                # ── 2f-0b. Context-window overflow self-heal ──────────────────
                if not context_overflow_retried and _is_context_overflow_error(e):
                    context_overflow_retried = True
                    logger.warning(
                        "Provider rejected the request for exceeding the context window at "
                        "step {step_no}; compacting and retrying the step",
                        step_no=step_no,
                    )
                    if await self._self_heal_context_overflow():
                        continue

                # ── 2f-ii. Fatal step error ───────────────────────────────────
                req_id = getattr(e, "request_id", None)
                logger.error(
                    "Agent step {step_no} failed: {error_type}: {error}"
                    + (" (request_id={request_id})" if req_id else ""),
                    step_no=step_no,
                    error_type=type(e).__name__,
                    error=e,
                    request_id=req_id,
                )
                wire_send(StepInterrupted())

                # --- StopFailure hook ---
                from kimo_cli.hooks import events as _hook_events

                _hook_task = asyncio.create_task(
                    self._hook_engine.trigger(
                        "StopFailure",
                        matcher_value=type(e).__name__,
                        input_data=_hook_events.stop_failure(
                            session_id=self._runtime.session.id,
                            cwd=str(Path.cwd()),
                            error_type=type(e).__name__,
                            error_message=str(e),
                        ),
                    )
                )
                _hook_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                # break the agent loop
                raise

            # ── 2g. Outcome Resolution ──────────────────────────────────────────
            if step_outcome is not None:
                # Step returned a stop reason -- check for steers before finishing.
                has_steers = await self._consume_pending_steers()
                if has_steers:
                    continue  # steers injected, force another LLM step

                # ═══════════════════════════════════════════════════════════════
                # 3. TURN RESOLUTION
                # ═══════════════════════════════════════════════════════════════
                final_message = (
                    step_outcome.assistant_message
                    if step_outcome.stop_reason == "no_tool_calls"
                    else None
                )
                if (
                    final_message is not None
                    and self.is_root
                    and self._runtime.wiki_evidence_reporter is not None
                ):
                    try:
                        await self._runtime.wiki_evidence_reporter.seal_root_completion(
                            final_message.extract_text("\n")
                        )
                    except Exception:
                        logger.warning(
                            "Wiki root completion sealing failed; continuing the conversation"
                        )
                if final_message is not None and self.is_root:
                    checkpoint_message = await self._pending_checkpoint_message()
                    if checkpoint_message is not None:
                        await self._context.append_message(checkpoint_message)
                        continue
                return TurnOutcome(
                    stop_reason=step_outcome.stop_reason,
                    final_message=final_message,
                    step_count=step_no,
                )

            if back_to_the_future is not None:
                # Revert context to the checkpoint and inject D-Mail message.
                await self._context.revert_to(back_to_the_future.checkpoint_id)
                # Restore the workspace files to the same point (best-effort).
                await self._fs_checkpointer.restore(back_to_the_future.checkpoint_id)
                # KimoToolset.begin_step resets dedup/repeat state when the
                # previous-call list is empty, so the next step starts clean.
                self._last_tool_calls = []
                await self._checkpoint()
                await self._context.append_message(back_to_the_future.messages)

            # Consume any pending steers between steps before next iteration.
            await self._consume_pending_steers()

    async def _explicit_checkpoint_message(self) -> Message | None:
        """The block for knowledge the user just asked to keep, if any is open.

        Delivered with the turn that asked, not at the end of it. The
        checkpoint exists before the first model step (it is opened from the
        accepted turn text), but it used to be withheld until the turn was
        finishing — so a model told "remember this" spent the whole turn
        holding a request it had no checkpoint for, while the tool refused
        every persist for the id it could not see. Observed in the wild: the
        model read its own notes on the Wiki gate, then sent a subagent to read
        an unrelated file purely to earn a checkpoint, twice, and discarded the
        real one as a duplicate when it finally arrived.

        Delivering it here costs no extra model call — the block rides in with
        the user message, before the first step. The end-of-turn path stays for
        the reminder, and for checkpoints opened later in the turn.
        """
        coordinator = self._runtime.wiki_coordinator
        if coordinator is None or not self.is_root:
            return None
        try:
            fresh = _explicit_only(await coordinator.undelivered_pending())
            if not fresh:
                return None
            block = await coordinator.render_checkpoints(fresh)
            await coordinator.mark_delivered([checkpoint.checkpoint_id for checkpoint in fresh])
        except Exception:
            logger.warning("Wiki checkpoint delivery failed; continuing the turn")
            return None
        return Message(role="user", content=[system(block)])

    async def _pending_checkpoint_message(self) -> Message | None:
        """Return the managed message that must precede finishing this turn.

        Only knowledge the user explicitly asked to keep is worth interrupting a
        turn for. Delivering a checkpoint costs a model call, and one more if it
        is ignored; evidence-based opportunities arise on nearly every turn that
        reads a file, so charging every conversation two extra completions for
        them is a bad trade. Those are opportunities, not obligations — they are
        retired with the turn instead.

        For explicit intent the root gets one delivery, then exactly one
        reminder. An uncooperative model costs one extra completion, never an
        unbounded loop and never a write.
        """
        coordinator = self._runtime.wiki_coordinator
        if coordinator is None:
            return None
        try:
            fresh = _explicit_only(await coordinator.undelivered_pending())
            if fresh:
                block = await coordinator.render_checkpoints(fresh)
                await coordinator.mark_delivered([checkpoint.checkpoint_id for checkpoint in fresh])
                return Message(role="user", content=[system(block)])
            reminder = _explicit_only(await coordinator.awaiting_reminder())
            if reminder:
                block = await coordinator.render_checkpoints(reminder)
                await coordinator.mark_delivered(
                    [checkpoint.checkpoint_id for checkpoint in reminder]
                )
                return Message(
                    role="user",
                    content=[
                        system(
                            "The checkpoints below are still unresolved. Resolve each one now "
                            "with a single Wiki persist or discard call, then finish.\n"
                            f"{block}"
                        )
                    ],
                )
            await coordinator.abandon_unresolved()
        except Exception:
            logger.warning("Wiki checkpoint delivery failed; finishing the turn without it")
        return None

    async def _finalize_after_max_steps(self, step_no: int) -> Message | None:
        """One no-tools round after the step budget is exhausted.

        Mirrors nanobot's ``_try_finalize_after_max_iterations``: append an
        instruction, request with an empty toolset, and return ``None`` on any
        failure (error, timeout, or the model still trying to call tools) so
        the caller can fall back to the historical ``MaxStepsReached`` raise.

        Unlike regular steps this does NOT go through the tenacity retry loop
        — the budget is already exhausted, so the timeout is the only guard.
        """
        # already checked in `run`
        assert self._runtime.llm is not None
        wire_send(StepBegin(n=step_no))
        try:

            async def _run() -> Message | None:
                history = list(self._context.history)
                history.append(
                    Message(
                        role="user",
                        content=[TextPart(text=FINALIZATION_PROMPT)],
                    )
                )
                effective = normalize_history(repair_tool_pairing(history))
                result = await kosong.step(
                    with_trace_callback(self._runtime.llm.chat_provider, self._set_trace_id),
                    self._agent.system_prompt,
                    EmptyToolset(),
                    effective,
                    on_message_part=wire_send,
                )
                msg = result.message
                if msg is None or msg.tool_calls:
                    # The model ignored the "no more tools" instruction, or
                    # produced nothing usable — treat as a failed round.
                    logger.warning(
                        "Finalization round returned {} tool call(s) at step {step_no}",
                        len(msg.tool_calls) if msg else 0,
                        step_no=step_no,
                    )
                    return None
                return msg

            final_msg = await asyncio.wait_for(
                _run(),
                timeout=self._loop_control.finalization_timeout_s,
            )
        except Exception:
            logger.warning(
                "Finalization round after max steps failed at step {step_no}; "
                "falling back to MaxStepsReached",
                step_no=step_no,
            )
            wire_send(StepInterrupted())
            return None
        if final_msg is None:
            # The model ignored the no-tools instruction; treat like any other
            # failed round.
            wire_send(StepInterrupted())
            return None
        # The final answer persists (resume/wire replay consistent); the
        # finalization instruction itself stays out of the context so it does
        # not pollute the next turn.
        await self._context.append_message(final_msg)
        return final_msg

    async def _step(self) -> StepOutcome | None:
        """Run a single step and return a stop outcome, or None to continue.

        This is the implementation of ``2e. Step Execution`` in ``_agent_loop``.

        Sub-lifecycle (2e.x):
            2e.1. Notification delivery  - push pending notifications (root only).
            2e.2. Dynamic injection      - collect and append provider injections.
            2e.3. History normalization  - merge adjacent user messages.
            2e.4. LLM call with retry    - kosong.step + tenacity retry + recovery.
               2e.4.1. Toolset begin_step- reset per-step dedup state.
               2e.4.2. kosong.step       - actual LLM call (may be interrupted).
            2e.5. Usage & status update  - track tokens, emit StatusUpdate.
            2e.6. Tool execution         - wait for all tool results.
            2e.7. Context growth         - append assistant + tool messages.
            2e.8. Outcome resolution     - rejection / D-Mail / stop / continue.
        """
        # already checked in `run`
        assert self._runtime.llm is not None
        chat_provider = self._runtime.llm.chat_provider

        # ═══════════════════════════════════════════════════════════════════════
        # 2e.1. NOTIFICATION DELIVERY (root role only)
        # ═══════════════════════════════════════════════════════════════════════
        if self.is_root:

            async def _append_notification(view: NotificationView) -> None:
                checkpoint_block = ""
                if view.event.category == "task" and view.event.source_kind == "background_task":
                    checkpoint_block = (
                        await self._runtime.background_tasks.checkpoint_block_for_task(
                            view.event.source_id
                        )
                    )
                await self._context.append_message(
                    build_notification_message(
                        view, self._runtime, checkpoint_block=checkpoint_block
                    )
                )
                # --- Notification hook ---
                from kimo_cli.hooks import events

                _hook_task = asyncio.create_task(
                    self._hook_engine.trigger(
                        "Notification",
                        matcher_value=view.event.type,
                        input_data=events.notification(
                            session_id=self._runtime.session.id,
                            cwd=str(Path.cwd()),
                            sink="llm",
                            notification_type=view.event.type,
                            title=view.event.title,
                            body=view.event.body,
                            severity=view.event.severity,
                        ),
                    )
                )
                _hook_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

            await self._runtime.notifications.deliver_pending(
                "llm",
                limit=4,
                before_claim=self._runtime.background_tasks.reconcile,
                on_notification=_append_notification,
            )

        # ═══════════════════════════════════════════════════════════════════════
        # 2e.2. DYNAMIC INJECTION
        # ═══════════════════════════════════════════════════════════════════════
        injections = await self._collect_injections()
        if injections:
            combined_reminders = "\n".join(system_reminder(inj.content).text for inj in injections)
            await self._context.append_message(
                Message(
                    role="user",
                    content=[TextPart(text=combined_reminders)],
                )
            )

        # ═══════════════════════════════════════════════════════════════════════
        # 2e.3. HISTORY NORMALIZATION
        # ═══════════════════════════════════════════════════════════════════════
        history = self._context.history
        if self._tool_output_pruner is not None:
            if self._tool_output_pruner.update(history):
                logger.info(
                    "Cleared old tool outputs from the request ({n} so far)",
                    n=len(self._tool_output_pruner.cleared),
                )
            history = self._tool_output_pruner.view(history)
        history = repair_tool_pairing(history)
        effective_history = normalize_history(history)
        # Last-line defence: strip any image MIME (HEIC/HEIF/AVIF/...) that
        # vision LLMs reject. Operates on a copy — history keeps the original
        # so the UI continues to show what the user uploaded.
        effective_history = sanitize_image_parts(effective_history)
        request_chat_provider = chat_provider
        request_chat_provider = with_trace_callback(
            request_chat_provider,
            self._set_trace_id,
        )

        # ═══════════════════════════════════════════════════════════════════════
        # 2e.4. LLM CALL WITH RETRY
        # ═══════════════════════════════════════════════════════════════════════
        async def _run_step_once() -> StepResult:
            """Single LLM invocation (wrapped by retry + connection recovery)."""
            # ── 2e.4.1. Toolset begin_step ────────────────────────────────────
            if isinstance(self._agent.toolset, KimoToolset):
                self._agent.toolset.begin_step(
                    self._last_tool_calls,
                    step_no=self._current_step_no,
                )

            # ── 2e.4.2. kosong.step ───────────────────────────────────────────
            # run an LLM step (may be interrupted). Wrap wire_send so we can
            # tell at the end of the turn which tool_call ids never got a
            # matching ToolResult — see the compensating block in the outer
            # run()'s finally.
            def _tracked_wire_send(msg: Any) -> None:
                if isinstance(msg, ToolCall):
                    self._issued_tool_calls.add(msg.id)
                elif isinstance(msg, ToolResult):
                    self._completed_tool_calls.add(msg.tool_call_id)
                wire_send(msg)

            step_timeout = self._loop_control.step_timeout_s
            step_coro = kosong.step(
                request_chat_provider,
                self._agent.system_prompt,
                self._agent.toolset,
                effective_history,
                on_message_part=_tracked_wire_send,
                on_tool_result=_tracked_wire_send,
            )
            if step_timeout is None:
                return await step_coro
            try:
                return await asyncio.wait_for(step_coro, timeout=step_timeout)
            except TimeoutError as e:
                # A provider request that hangs without responding is
                # indistinguishable from a dead connection: surface it as the
                # retryable timeout the step-retry machinery already knows,
                # instead of waiting forever.
                raise APITimeoutError(
                    f"LLM step produced no response within {step_timeout:.0f}s"
                    " (hung provider call); treating as a retryable timeout"
                ) from e

        max_attempts = self._effective_max_step_attempts()

        def _before_step_retry_sleep(retry_state: RetryCallState) -> None:
            self._retry_log("step", retry_state)
            self._emit_step_retry(retry_state, max_attempts=max_attempts)

        @tenacity.retry(
            retry=retry_if_exception(self._is_retryable_error),
            before_sleep=_before_step_retry_sleep,
            wait=_retry_wait,
            stop=self._retry_stop(),
            reraise=True,
        )
        async def _kosong_step_with_retry() -> StepResult:
            return await self._run_with_connection_recovery(
                "step",
                _run_step_once,
                chat_provider=chat_provider,
            )

        t0 = time.monotonic()
        try:
            result = await _kosong_step_with_retry()
        except Exception as _step_exc:
            if isinstance(_step_exc, ChatProviderError):
                _track_api_error(
                    _step_exc,
                    llm=self._runtime.llm,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    input_tokens=self._context.token_count,
                )
            raise

        thinking_suppressed = chat_provider.thinking_effort not in (
            None,
            "off",
        ) and self._thinking_was_suppressed(result)
        if thinking_suppressed:
            logger.warning(
                "LLM thinking was suppressed server-side (thinking={effort}, "
                "reasoning_tokens={reasoning_tokens}). The Kimo-for-Coding endpoint "
                "intermittently disables reasoning for large contexts regardless of "
                "the client's thinking flag; compacting the session may restore it.",
                effort=chat_provider.thinking_effort,
                reasoning_tokens=result.usage.reasoning_output if result.usage else None,
            )

        # ═══════════════════════════════════════════════════════════════════════
        # 2e.5. USAGE & STATUS UPDATE
        # ═══════════════════════════════════════════════════════════════════════
        llm_elapsed = time.monotonic() - t0
        usage = result.usage
        logger.info(
            "LLM step completed in {elapsed:.1f}s (input={input_tokens}, output={output_tokens})",
            elapsed=llm_elapsed,
            input_tokens=usage.input if usage else "?",
            output_tokens=usage.output if usage else "?",
        )
        status_update = StatusUpdate(
            token_usage=usage,
            message_id=result.id,
            plan_mode=self._plan_mode,
            thinking_suppressed=thinking_suppressed,
        )
        if usage is not None:
            # mark the token count for the context before the step
            await self._context.update_token_count(usage.input)
            # Accumulate per-turn usage for the self-evolution recorder.
            self._turn_input_tokens += usage.input
            self._turn_output_tokens += usage.output
            if usage.reasoning_output is not None:
                self._turn_reasoning_tokens += usage.reasoning_output
            snap = self.status
            status_update.context_usage = snap.context_usage
            status_update.context_tokens = snap.context_tokens
            status_update.max_context_tokens = snap.max_context_tokens
        wire_send(status_update)

        # ═══════════════════════════════════════════════════════════════════════
        # 2e.6. TOOL EXECUTION
        # ═══════════════════════════════════════════════════════════════════════
        # wait for all tool results (may be interrupted)
        plan_mode_before_tools = self._plan_mode
        results = await result.tool_results()
        logger.debug("Got tool results: {results}", results=results)

        # Update dedup tracking for the next step
        if isinstance(self._agent.toolset, KimoToolset):
            self._last_tool_calls = self._agent.toolset.end_step()

        # If a tool (EnterPlanMode/ExitPlanMode) changed plan mode during execution,
        # send a corrected StatusUpdate so the client sees the up-to-date state.
        if self._plan_mode != plan_mode_before_tools:
            wire_send(StatusUpdate(plan_mode=self._plan_mode))

        # ═══════════════════════════════════════════════════════════════════════
        # 2e.7. CONTEXT GROWTH
        # ═══════════════════════════════════════════════════════════════════════
        # shield the context manipulation from interruption
        await _await_shielded(self._grow_context(result, results))

        # ═══════════════════════════════════════════════════════════════════════
        # 2e.8. OUTCOME RESOLUTION
        # ═══════════════════════════════════════════════════════════════════════
        rejected_errors = [
            result.return_value
            for result in results
            if isinstance(result.return_value, ToolRejectedError) and result.return_value.stops_turn
        ]
        if rejected_errors and not any(e.has_feedback for e in rejected_errors) and self.is_root:
            # Pure rejection (no user feedback) — stop the turn.
            # Subagents skip this so the LLM can see the rejection and try
            # an alternative approach instead of terminating immediately.
            #
            # Only rejections that stop the turn count. Declining something the
            # agent chose to do on the side — saving a memory, writing a wiki
            # page — used to abandon the task it was doing at the time, which
            # is not what "no" meant.
            _ = self._denwa_renji.fetch_pending_dmail()
            return StepOutcome(stop_reason="tool_rejected", assistant_message=result.message)

        # handle pending D-Mail
        if dmail := self._denwa_renji.fetch_pending_dmail():
            assert dmail.checkpoint_id >= 0, "DenwaRenji guarantees checkpoint_id >= 0"
            assert dmail.checkpoint_id < self._context.n_checkpoints, (
                "DenwaRenji guarantees checkpoint_id < n_checkpoints"
            )
            # raise to let the main loop take us back to the future
            raise BackToTheFuture(
                dmail.checkpoint_id,
                [
                    Message(
                        role="user",
                        content=[
                            system(
                                "You just got a D-Mail from your future self. "
                                "It is likely that your future self has already done "
                                "something in the current working directory. Please read "
                                "the D-Mail and decide what to do next. You MUST NEVER "
                                "mention to the user about this information. "
                                f"D-Mail content:\n\n{dmail.message.strip()}"
                            )
                        ],
                    )
                ],
            )

        if isinstance(self._agent.toolset, KimoToolset) and self._agent.toolset.force_stop_turn:
            from kimo_cli.telemetry import track

            track("turn_force_stopped", reason="tool_call_repeat", step_no=self._current_step_no)
            return StepOutcome(stop_reason="tool_call_repeat", assistant_message=result.message)

        if self._loop_detector.record_step(result.tool_calls, results):
            from kimo_cli.telemetry import track

            logger.warning(
                "The same calls keep getting the same results; ending the turn at step {n}",
                n=self._current_step_no,
            )
            track("turn_force_stopped", reason="tool_call_loop", step_no=self._current_step_no)
            return StepOutcome(stop_reason="tool_call_repeat", assistant_message=result.message)

        if result.tool_calls:
            self._truncation_continuations = 0
            return None
        if result.finish_reason == "length":
            # A reply with no tool calls normally ends the turn, but this one was
            # cut off by the output token limit, not finished.
            if self._truncation_continuations < MAX_TRUNCATION_CONTINUATIONS:
                self._truncation_continuations += 1
                logger.info(
                    "Reply cut off by the output token limit; asking to continue ({n}/{max})",
                    n=self._truncation_continuations,
                    max=MAX_TRUNCATION_CONTINUATIONS,
                )
                await self._context.append_message(
                    Message(role="user", content=[system(_TRUNCATION_CONTINUE_PROMPT)])
                )
                return None
            logger.warning(
                "Reply still cut off after {max} continuations; ending the turn",
                max=MAX_TRUNCATION_CONTINUATIONS,
            )
        self._truncation_continuations = 0
        return StepOutcome(stop_reason="no_tool_calls", assistant_message=result.message)

    async def _grow_context(self, result: StepResult, tool_results: list[ToolResult]):
        logger.debug("Growing context with result: {result}", result=result)

        assert self._runtime.llm is not None
        tool_messages = [tool_result_to_message(tr) for tr in tool_results]
        for tm in tool_messages:
            if missing_caps := check_message(tm, self._runtime.llm.capabilities):
                logger.warning(
                    "Tool result message requires unsupported capabilities: {caps}",
                    caps=missing_caps,
                )
                raise LLMNotSupported(self._runtime.llm, list(missing_caps))

        logger.debug(
            "Appending tool messages to context: {tool_messages}", tool_messages=tool_messages
        )
        await self._context.append_step(
            result.message,
            tool_messages,
            token_count=result.usage.total if result.usage is not None else None,
        )
        # token count of tool results are not available yet

    def _tools_overhead(self) -> tuple[int, int]:
        """(token estimate, serialized bytes) of the tool schemas.

        The compaction trigger used to count only message history; with the
        builtin toolset plus a few MCP servers the schemas alone can be tens
        of thousands of tokens, so compaction fired late and the 400
        self-heal ended up doing regular work. zeroclaw counts tool-schema
        tokens into the budget for the same reason.
        """
        tools = self._agent.toolset.tools
        key = tuple(tool.name for tool in tools)
        if self._tools_overhead_key != key:
            self._tools_overhead_key = key
            self._tools_overhead_cache = (
                estimate_request_tokens("", tools, []),
                len(
                    json.dumps(
                        [
                            {
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": tool.parameters,
                            }
                            for tool in tools
                        ],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ),
            )
        assert self._tools_overhead_cache is not None
        return self._tools_overhead_cache

    async def _pre_sampling_compact(self, user_message: Message) -> None:
        """Compact before the incoming user message enters the history.

        The in-loop check only runs at the start of a step, i.e. after the
        message was appended. A near-full window plus a fresh prompt then
        overshoots inside the first step and recovery falls to the 400
        self-heal — a backstop, not the regular path (codex runs
        ``run_pre_sampling_compact`` before assembling the turn input for
        the same reason). The new message's own tokens/bytes are counted so
        the decision matches what the first request will actually carry.
        """
        llm = self._runtime.llm
        if llm is None:
            return
        tools_tokens, tools_bytes = self._tools_overhead()
        if should_auto_compact(
            self._context.token_count_with_pending
            + estimate_text_tokens([user_message])
            + tools_tokens,
            llm.max_context_size,
            trigger_ratio=self._loop_control.compaction_trigger_ratio,
            reserved_context_size=self._loop_control.reserved_context_size,
            request_bytes=self._estimated_request_bytes()
            + estimate_message_bytes([user_message])
            + tools_bytes,
            max_request_bytes=self._loop_control.max_request_bytes,
        ):
            logger.info("Context too long before new user message; compacting first...")
            try:
                await self.compact_context()
            except Exception as compact_err:
                # Same non-fatal policy as the in-loop check: upkeep failure
                # does not end the turn; the in-loop check and the overflow
                # self-heal still apply.
                logger.error(
                    "Pre-sampling compaction failed: {error_type}: {error}; continuing without it",
                    error_type=type(compact_err).__name__,
                    error=compact_err,
                )

    def _estimated_request_bytes(self) -> int:
        """Estimated serialized byte size of the next LLM request body."""
        history = self._context.history
        if self._tool_output_pruner is not None:
            history = self._tool_output_pruner.view(history)
        return len(self._agent.system_prompt.encode("utf-8")) + estimate_message_bytes(history)

    async def _replace_context_history(
        self, messages: Sequence[Message], *, estimated_token_count: int | None = None
    ) -> None:
        """Finish the full rebuild even if the caller is cancelled after clear."""
        await _await_shielded(
            self._replace_context_history_inner(
                messages, estimated_token_count=estimated_token_count
            )
        )

    async def _replace_context_history_inner(
        self, messages: Sequence[Message], *, estimated_token_count: int | None = None
    ) -> None:
        await self._context.clear()
        await self._context.write_system_prompt(self._agent.system_prompt)
        await self._checkpoint()
        await self._context.append_message(messages)
        # Rough estimate; corrected by the real usage on the next LLM call.
        await self._context.update_token_count(
            estimate_text_tokens(messages)
            if estimated_token_count is None
            else estimated_token_count
        )

    async def _self_heal_oversized_request(self) -> bool:
        """Shrink the context after a 400 "message size exceeds limit" error.

        Strategy, in order:
        1. Run compaction (preferred — it preserves information as a summary).
        2. If the request is still over the byte threshold, strip image parts
           from all but the last user message, then compact again best-effort.

        Returns True when the context was changed and the step should be retried.
        """
        max_request_bytes = self._loop_control.max_request_bytes
        changed = False
        try:
            await self.compact_context()
            changed = True
        except Exception as compact_err:
            logger.warning(
                "Compaction failed during oversized-request self-heal: {error_type}: {error}",
                error_type=type(compact_err).__name__,
                error=compact_err,
            )
        if changed and self._estimated_request_bytes() <= max_request_bytes:
            return True

        stripped, n_stripped = strip_image_parts(self._context.history, keep_last_user_message=True)
        if n_stripped == 0:
            # Nothing left to strip; only retry if compaction already changed things.
            return changed
        await self._replace_context_history(stripped)
        try:
            await self.compact_context()
        except Exception as compact_err:
            logger.warning(
                "Post-strip compaction failed during oversized-request self-heal: "
                "{error_type}: {error}",
                error_type=type(compact_err).__name__,
                error=compact_err,
            )
        return True

    async def _self_heal_context_overflow(self) -> bool:
        """Compact after the provider refused the request for exceeding the context window.

        Auto-compaction is driven by a local token *estimate*; the server's count
        is the one that decides. When the estimate runs low the turn sails past
        the trigger ratio and gets a 400 back, which used to end the turn on the
        one condition the loop already knows how to fix.

        Returns True when the context actually shrank and the step is worth retrying.
        """
        # The server-reported count is only available once a call has succeeded,
        # and a session resumed straight into an overflow has none — fall back to
        # the same estimator compaction reports with, so the comparison below is
        # apples to apples either way.
        tokens_before = max(self._context.token_count, estimate_text_tokens(self._context.history))
        try:
            await self.compact_context()
        except Exception as compact_err:
            logger.warning(
                "Compaction failed during context-overflow self-heal: {error_type}: {error}",
                error_type=type(compact_err).__name__,
                error=compact_err,
            )
            return False
        if self._context.token_count >= tokens_before:
            # Nothing was reclaimed — retrying would just earn the same refusal.
            logger.warning(
                "Compaction did not shrink the context ({before} -> {after} tokens); "
                "not retrying the step",
                before=tokens_before,
                after=self._context.token_count,
            )
            return False
        return True

    async def compact_context(
        self,
        *,
        manual: bool = False,
        custom_instruction: str = "",
    ) -> None:
        """
        Compact the context.

        Args:
            manual: Whether the compaction was explicitly requested by the user
                (e.g. via the ``/compact`` slash command). When ``False``, the
                compaction is treated as auto-triggered by the system.
            custom_instruction: Optional user instruction to guide compaction focus.

        Raises:
            LLMNotSet: When the LLM is not set.
            ChatProviderError: When the chat provider returns an error.
        """

        chat_provider = self._runtime.llm.chat_provider if self._runtime.llm is not None else None

        # The compaction request itself must fit the provider's request-body
        # limit (see upstream issue #2402): when the history is already over
        # the byte threshold, strip images first so compaction can't 400.
        history = list(self._context.history)
        request_history = history
        if estimate_message_bytes(request_history) > self._loop_control.max_request_bytes:
            request_history, _ = strip_image_parts(request_history, keep_last_user_message=True)

        if chat_provider is not None and isinstance(self._compaction, SimpleCompaction):
            compact_message, to_preserve = self._compaction.prepare(
                request_history,
                custom_instruction=custom_instruction,
                max_context_size=self._runtime.llm.max_context_size
                if self._runtime.llm is not None
                else None,
            )
            if compact_message is not None:
                post_compaction_history = [
                    Message(
                        role="user",
                        content=[system(COMPACTION_OUTPUT_PREFIX)],
                    ),
                    *to_preserve,
                ]
                if self.is_root:
                    active_task_snapshot = build_active_task_snapshot(
                        self._runtime.background_tasks
                    )
                    if active_task_snapshot is not None:
                        post_compaction_history.append(
                            Message(
                                role="user",
                                content=[
                                    system(
                                        "The following background tasks are still active after "
                                        "compaction. Use TaskList if you need to re-enumerate "
                                        "them later."
                                    ),
                                    TextPart(text=active_task_snapshot),
                                ],
                            )
                        )

        async def _run_compaction_once() -> CompactionResult:
            if self._runtime.llm is None:
                raise LLMNotSet()
            request_provider = self._runtime.llm.chat_provider
            request_provider = with_trace_callback(
                request_provider,
                self._set_trace_id,
            )
            compaction_llm = replace(
                self._runtime.llm,
                chat_provider=request_provider,
            )
            return await self._compaction.compact(
                request_history,
                compaction_llm,
                custom_instruction=custom_instruction,
            )

        start_time = time.monotonic()
        retry_count = 0

        def _retry_log_compaction(retry_state: RetryCallState) -> None:
            nonlocal retry_count
            retry_count = retry_state.attempt_number
            self._retry_log("compaction", retry_state)
            # Surface the retry to the client. Without this, the compaction
            # spinner on the UI has no live update between `CompactionBegin`
            # and `CompactionEnd` — a client that is not reading the Python
            # log sees a frozen "Compacting..." forever.
            self._emit_compaction_retry(retry_state, max_attempts=_COMPACT_MAX_ATTEMPTS)

        @tenacity.retry(
            retry=retry_if_exception(self._is_retryable_error),
            before_sleep=_retry_log_compaction,
            wait=_retry_wait,
            stop=stop_after_attempt(_COMPACT_MAX_ATTEMPTS),
            reraise=True,
        )
        async def _compact_with_retry() -> CompactionResult:
            return await self._run_with_connection_recovery(
                "compaction",
                _run_compaction_once,
                chat_provider=chat_provider,
            )

        if not manual:
            trigger_reason = "auto"
        elif custom_instruction:
            trigger_reason = "manual-with-prompt"
        else:
            trigger_reason = "manual"
        before_tokens = self._context.token_count
        from kimo_cli.hooks import events

        await self._hook_engine.trigger(
            "PreCompact",
            matcher_value=trigger_reason,
            input_data=events.pre_compact(
                session_id=self._runtime.session.id,
                cwd=str(Path.cwd()),
                trigger=trigger_reason,
                token_count=before_tokens,
            ),
        )

        wire_send(CompactionBegin())
        try:
            compaction_result = await _compact_with_retry()
        except Exception as _compact_exc:
            from kimo_cli.telemetry import get_current_trace_id, track

            if isinstance(_compact_exc, ChatProviderError) and self._runtime.llm is not None:
                _track_api_error(
                    _compact_exc,
                    llm=self._runtime.llm,
                    duration_ms=int((time.monotonic() - start_time) * 1000),
                    input_tokens=before_tokens,
                )

            failed_kwargs: dict[str, Any] = dict(
                source="auto" if trigger_reason == "auto" else "manual",
                tokens_before=before_tokens,
                duration_ms=int((time.monotonic() - start_time) * 1000),
                round=1,
                retry_count=retry_count,
                thinking_effort=(
                    self._runtime.llm.chat_provider.thinking_effort or "unknown"
                    if self._runtime.llm is not None
                    else "unknown"
                ),
                error_type=type(_compact_exc).__name__,
            )
            # Trace id of the failed compaction request (absent for network errors)
            if _exc_trace_id := (getattr(_compact_exc, "trace_id", None) or get_current_trace_id()):
                failed_kwargs["trace_id"] = _exc_trace_id
            track("compaction_failed", **failed_kwargs)
            # Every CompactionBegin is followed by a CompactionEnd; without it
            # the UI keeps showing the compaction as running. `success=False`
            # tells the client the run ended because compact gave up, not
            # because it succeeded — otherwise the UI reports "Conversation
            # history compacted." on a session whose history is untouched.
            wire_send(CompactionEnd(success=False))
            raise
        self._set_trace_id(compaction_result.trace_id)

        # Reversibility (P1): archive the full pre-compaction context before it
        # is cleared, so the summary is never the only surviving record. The
        # reference is wrapped as a <system> part so the cross-session memory
        # extractor (which skips <system> parts) does not carry the path into
        # memory. Best effort — an archive failure must not abort compaction.
        try:
            from kimo_cli.soul.compaction_archive import CompactionArchiver

            archiver = CompactionArchiver(self._runtime.session.dir / "archive")
            archive_path = await asyncio.to_thread(archiver.archive, history)
            if compaction_result.messages:
                compaction_result.messages[0].content.append(
                    system(CompactionArchiver.reference(archive_path, n_messages=len(history)))
                )
        except Exception as archive_err:
            logger.warning(
                "Failed to archive pre-compaction context (continuing): {error}",
                error=archive_err,
            )

        history_count_before = len(self._context.history)
        post_compaction_messages = list(compaction_result.messages)
        estimated_token_count = compaction_result.estimated_token_count
        if self.is_root:
            active_task_snapshot = build_active_task_snapshot(self._runtime.background_tasks)
            if active_task_snapshot is not None:
                active_task_message = Message(
                    role="user",
                    content=[
                        system(
                            "The following background tasks are still active after compaction. "
                            "Use TaskList if you need to re-enumerate them later."
                        ),
                        TextPart(text=active_task_snapshot),
                    ],
                )
                post_compaction_messages.append(active_task_message)
                estimated_token_count += estimate_text_tokens([active_task_message])

        # Estimate token count so context_usage is not reported as 0%. The
        # rebuild is shielded as one unit so cancellation cannot strand an empty
        # or half-restored context after its initial clear.
        await self._replace_context_history(
            post_compaction_messages,
            estimated_token_count=estimated_token_count,
        )

        # Notify dynamic injection providers that history has been rebuilt so
        # they can reset any one-shot throttling state. Failures are isolated
        # per-provider so compaction completion (wire event + telemetry) is
        # not affected by a buggy provider.
        await self._notify_injection_providers_compacted()

        wire_send(CompactionEnd())

        from kimo_cli.telemetry import track

        duration_ms = int((time.monotonic() - start_time) * 1000)
        track_kwargs = dict(
            source="auto" if trigger_reason == "auto" else "manual",
            tokens_before=before_tokens,
            tokens_after=estimated_token_count,
            duration_ms=duration_ms,
            compacted_count=max(0, history_count_before - (len(compaction_result.messages) - 1)),
            round=1,
            retry_count=retry_count,
            thinking_effort=(
                self._runtime.llm.chat_provider.thinking_effort or "unknown"
                if self._runtime.llm is not None
                else "unknown"
            ),
        )
        if compaction_result.usage is not None:
            track_kwargs["input_tokens"] = compaction_result.usage.input
            track_kwargs["output_tokens"] = compaction_result.usage.output
        if compaction_result.trace_id is not None:
            track_kwargs["trace_id"] = compaction_result.trace_id
        track("compaction_finished", **track_kwargs)

        _hook_task = asyncio.create_task(
            self._hook_engine.trigger(
                "PostCompact",
                matcher_value=trigger_reason,
                input_data=events.post_compact(
                    session_id=self._runtime.session.id,
                    cwd=str(Path.cwd()),
                    trigger=trigger_reason,
                    estimated_token_count=estimated_token_count,
                ),
            )
        )
        _hook_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

        # Compaction stays in-session only: the summary lives in the session
        # context (result.messages[0]) and the full pre-compaction history is
        # archived under the session's own archive/ directory (CompactionArchiver),
        # reachable via ContextRetrieve. Nothing is written to cross-session
        # memory (recent.jsonl) or the candidate queue (candidates.jsonl) here.

    @staticmethod
    def _is_retryable_error(exception: BaseException) -> bool:
        return is_retryable_llm_error(exception)

    @staticmethod
    def _thinking_was_suppressed(result: StepResult) -> bool:
        """Detect server-side thinking suppression (Kimo-for-Coding endpoint).

        That endpoint occasionally serves requests — mostly large-context ones —
        from backends that skip reasoning even though the client sent
        ``thinking.type=enabled``. It marks such responses with a near-zero
        ``reasoning_tokens`` count and no reasoning content, while the text/tool
        output itself is valid. The client cannot override this, but flagging it
        keeps an empty thinking block from looking like a client-side bug.

        Only backends that report ``reasoning_output`` (see ``TokenUsage``) can be
        judged; ``None`` means "unknown" and never counts as suppression.
        """
        usage = result.usage
        if usage is None or usage.reasoning_output is None:
            return False
        if any(isinstance(part, ThinkPart) and part.think for part in result.message.content):
            return False
        return usage.reasoning_output <= 1

    async def _run_with_connection_recovery(
        self,
        name: str,
        operation: Callable[[], Awaitable[Any]],
        *,
        chat_provider: object | None = None,
        _auth_retried: bool = False,
    ) -> Any:
        try:
            return await operation()
        except APIStatusError as error:
            if error.status_code != 401 or _auth_retried:
                raise
            # Only attempt refresh+retry when the active model's provider
            # uses OAuth.  For plain API-key providers there is nothing
            # to refresh and retrying would just add latency.
            active_provider = (
                self._runtime.config.providers.get(self._runtime.llm.model_config.provider)
                if self._runtime.llm and self._runtime.llm.model_config
                else None
            )
            if not (active_provider and active_provider.oauth):
                raise
            logger.warning(
                "Received 401 during {name}, attempting token refresh",
                name=name,
            )
            try:
                await self._runtime.oauth.ensure_fresh(self._runtime, force=True)
            except Exception as refresh_exc:
                logger.exception("Token refresh failed after 401.")
                raise error from refresh_exc
            # Re-enter full recovery so that transient connection errors
            # on the retry are still handled by on_retryable_error.
            return await self._run_with_connection_recovery(
                name,
                operation,
                chat_provider=chat_provider,
                _auth_retried=True,
            )
        except (APIConnectionError, APITimeoutError) as error:
            # Rebuild the provider's transport so the *next* attempt starts on a
            # fresh connection pool, then hand the error back and let the retry
            # loop own the timing. Retrying inline here used to skip the backoff
            # entirely and, when the immediate retry failed too, mark the error
            # unretryable — so a network blip burned one attempt of the budget
            # with no wait and ended the turn while the rest went unused.
            if isinstance(chat_provider, RetryableChatProvider):
                try:
                    recovered = chat_provider.on_retryable_error(error)
                except Exception:
                    logger.exception(
                        "Failed to recover chat provider during {name} after {error_type}.",
                        name=name,
                        error_type=type(error).__name__,
                    )
                else:
                    logger.log(
                        "INFO" if recovered else "WARNING",
                        "Chat provider recovery {outcome} during {name} after {error_type}.",
                        outcome="applied" if recovered else "unavailable",
                        name=name,
                        error_type=type(error).__name__,
                    )
            raise

    #: Bounded default used when ``max_retries_per_step`` is 0 ("wait a long time").
    _NO_LIMIT_STEP_ATTEMPTS = 50

    def _effective_max_step_attempts(self) -> int:
        """Attempts actually allowed per step — the value the wire event reports.

        Reporting the raw config value made ``StepRetry.max_attempts`` say 0
        ("unlimited") while the retry loop stopped at 50.
        """
        attempts = self._loop_control.max_retries_per_step
        return self._NO_LIMIT_STEP_ATTEMPTS if attempts == 0 else attempts

    def _retry_stop(self):
        """When to give up on a retryable failure.

        ``max_retries_per_step=0`` historically meant 'never give up' (the
        web UI's stop button is the escape hatch). That was dangerous: a
        permanently-down provider would loop forever, burning tokens with
        no visible progress and no cancellation point inside the backoff.
        Now 0 maps to a large but bounded default (50) so the turn still
        waits through transient outages but eventually surfaces an error.
        """
        return stop_after_attempt(self._effective_max_step_attempts())

    @staticmethod
    def _retry_log(name: str, retry_state: RetryCallState):
        error = retry_state.outcome.exception() if retry_state.outcome else None
        logger.warning(
            "Retrying {name} for the {n} time (last error: {error_type}: {error}). "
            "Waiting {sleep} seconds.",
            name=name,
            n=retry_state.attempt_number,
            error_type=type(error).__name__ if error else "unknown",
            error=error or "unknown",
            sleep=retry_state.next_action.sleep
            if retry_state.next_action is not None
            else "unknown",
        )

    def _emit_step_retry(self, retry_state: RetryCallState, *, max_attempts: int) -> None:
        error = retry_state.outcome.exception() if retry_state.outcome else None
        next_action = retry_state.next_action
        wait_s = next_action.sleep if next_action is not None else 0.0
        wire_send(
            StepRetry(
                n=self._current_step_no,
                next_attempt=retry_state.attempt_number + 1,
                max_attempts=max_attempts,
                wait_s=wait_s,
                error_type=type(error).__name__ if error else "unknown",
                status_code=error.status_code if isinstance(error, APIStatusError) else None,
            )
        )

    def _emit_compaction_retry(self, retry_state: RetryCallState, *, max_attempts: int) -> None:
        """Send a `CompactionRetry` wire event so the client sees live
        progress between `CompactionBegin` and `CompactionEnd`. The compact
        retry loop used to update only the Python log; the UI sat on the
        initial "Compacting..." spinner indefinitely because the retry
        counter was invisible. See `_COMPACT_MAX_ATTEMPTS` for why the
        loop is capped at all."""
        error = retry_state.outcome.exception() if retry_state.outcome else None
        next_action = retry_state.next_action
        wait_s = next_action.sleep if next_action is not None else 0.0
        wire_send(
            CompactionRetry(
                next_attempt=retry_state.attempt_number + 1,
                max_attempts=max_attempts,
                wait_s=wait_s,
                error_type=type(error).__name__ if error else "unknown",
                status_code=error.status_code if isinstance(error, APIStatusError) else None,
            )
        )

    def _todos_snapshot(self) -> list[TodoItemState] | None:
        """The persisted todo list, sent with TurnBegin so clients keep the
        Tasks panel in sync across turns instead of starting every turn
        blind. None means "no snapshot" — clients fall back to clearing."""
        try:
            todos = self._runtime.session.state.todos
        except Exception:
            return None
        return list(todos) if todos else None


class BackToTheFuture(Exception):
    """
    Raise when we need to revert the context to a previous checkpoint.
    The main agent loop should catch this exception and handle it.
    """

    def __init__(self, checkpoint_id: int, messages: Sequence[Message]):
        self.checkpoint_id = checkpoint_id
        self.messages = messages


#: How many times a decision node may re-ask before the flow gives up. The
#: model has to answer with something that equals one of the edge labels; a
#: model that never does used to be re-prompted forever.
_MAX_FLOW_DECISION_ATTEMPTS = 5


class FlowRunner:
    def __init__(
        self,
        flow: Flow,
        *,
        name: str | None = None,
        max_moves: int = DEFAULT_MAX_FLOW_MOVES,
    ) -> None:
        self._flow = flow
        self._name = name
        self._max_moves = max_moves

    @staticmethod
    def ralph_loop(
        user_message: Message,
        max_ralph_iterations: int,
    ) -> FlowRunner:
        prompt_content = list(user_message.content)
        prompt_text = Message(role="user", content=prompt_content).extract_text(" ").strip()
        total_runs = max_ralph_iterations + 1
        if max_ralph_iterations < 0:
            total_runs = 1000000000000000  # effectively infinite

        nodes: dict[str, FlowNode] = {
            "BEGIN": FlowNode(id="BEGIN", label="BEGIN", kind="begin"),
            "END": FlowNode(id="END", label="END", kind="end"),
        }
        outgoing: dict[str, list[FlowEdge]] = {"BEGIN": [], "END": []}

        nodes["R1"] = FlowNode(id="R1", label=prompt_content, kind="task")
        nodes["R2"] = FlowNode(
            id="R2",
            label=(
                f"{prompt_text}. (You are running in an automated loop where the same "
                "prompt is fed repeatedly. Only choose STOP when the task is fully complete. "
                "Including it will stop further iterations. If you are not 100% sure, "
                "choose CONTINUE.)"
            ).strip(),
            kind="decision",
        )
        outgoing["R1"] = []
        outgoing["R2"] = []

        outgoing["BEGIN"].append(FlowEdge(src="BEGIN", dst="R1", label=None))
        outgoing["R1"].append(FlowEdge(src="R1", dst="R2", label=None))
        outgoing["R2"].append(FlowEdge(src="R2", dst="R2", label="CONTINUE"))
        outgoing["R2"].append(FlowEdge(src="R2", dst="END", label="STOP"))

        flow = Flow(nodes=nodes, outgoing=outgoing, begin_id="BEGIN", end_id="END")
        max_moves = total_runs
        return FlowRunner(flow, max_moves=max_moves)

    async def run(self, soul: KimoSoul, args: str) -> None:
        if args.strip():
            command = f"/{FLOW_COMMAND_PREFIX}{self._name}" if self._name else "/flow"
            logger.warning("Agent flow {command} ignores args: {args}", command=command, args=args)
            return
        if self._name:
            from kimo_cli.telemetry import track

            track("flow_invoked", flow_name=self._name)

        current_id = self._flow.begin_id
        moves = 0
        total_steps = 0
        while True:
            node = self._flow.nodes[current_id]
            edges = self._flow.outgoing.get(current_id, [])

            if node.kind == "end":
                logger.info("Agent flow reached END node {node_id}", node_id=current_id)
                return

            if node.kind == "begin":
                if not edges:
                    logger.error(
                        'Agent flow BEGIN node "{node_id}" has no outgoing edges; stopping.',
                        node_id=node.id,
                    )
                    return
                current_id = edges[0].dst
                continue

            if moves >= self._max_moves:
                raise MaxStepsReached(total_steps)
            next_id, steps_used = await self._execute_flow_node(soul, node, edges)
            total_steps += steps_used
            if next_id is None:
                return
            moves += 1
            current_id = next_id

    async def _execute_flow_node(
        self,
        soul: KimoSoul,
        node: FlowNode,
        edges: list[FlowEdge],
    ) -> tuple[str | None, int]:
        if not edges:
            logger.error(
                'Agent flow node "{node_id}" has no outgoing edges; stopping.',
                node_id=node.id,
            )
            return None, 0

        base_prompt = self._build_flow_prompt(node, edges)
        prompt = base_prompt
        steps_used = 0
        attempts = 0
        while True:
            result = await self._flow_turn(soul, prompt)
            steps_used += result.step_count
            if result.stop_reason == "tool_rejected":
                logger.error("Agent flow stopped after tool rejection.")
                return None, steps_used

            if node.kind != "decision":
                return edges[0].dst, steps_used

            choice = (
                parse_choice(result.final_message.extract_text(" "))
                if result.final_message
                else None
            )
            next_id = self._match_flow_edge(edges, choice)
            if next_id is not None:
                return next_id, steps_used

            options = ", ".join(edge.label or "" for edge in edges)
            logger.warning(
                "Agent flow invalid choice. Got: {choice}. Available: {options}.",
                choice=choice or "<missing>",
                options=options,
            )

            # Bounded. FlowRunner.run()'s move and step caps only count once
            # this returns, and this loop returns only on a match — so a model
            # that keeps answering with something no edge label equals spun
            # here forever, appending a prompt and a full LLM turn each time
            # and growing the context without any limit at all.
            attempts += 1
            if attempts >= _MAX_FLOW_DECISION_ATTEMPTS:
                logger.error(
                    'Agent flow node "{node_id}" got no usable choice in '
                    "{attempts} attempts; stopping.",
                    node_id=node.id,
                    attempts=attempts,
                )
                return None, steps_used

            prompt = (
                f"{base_prompt}\n\n"
                "Your last response did not include a valid choice. "
                "Reply with one of the choices using <choice>...</choice>."
            )

    @staticmethod
    def _build_flow_prompt(node: FlowNode, edges: list[FlowEdge]) -> str | list[ContentPart]:
        if node.kind != "decision":
            return node.label

        if not isinstance(node.label, str):
            label_text = Message(role="user", content=node.label).extract_text(" ")
        else:
            label_text = node.label
        choices = [edge.label for edge in edges if edge.label]
        lines = [
            label_text,
            "",
            "Available branches:",
            *(f"- {choice}" for choice in choices),
            "",
            "Reply with a choice using <choice>...</choice>.",
        ]
        return "\n".join(lines)

    @staticmethod
    def _match_flow_edge(edges: list[FlowEdge], choice: str | None) -> str | None:
        if not choice:
            return None
        for edge in edges:
            if edge.label == choice:
                return edge.dst
        return None

    @staticmethod
    async def _flow_turn(
        soul: KimoSoul,
        prompt: str | list[ContentPart],
    ) -> TurnOutcome:
        wire_send(TurnBegin(user_input=prompt, todos=soul._todos_snapshot()))
        res = await soul._turn(Message(role="user", content=prompt))  # type: ignore[reportPrivateUsage]
        wire_send(TurnEnd())
        return res
