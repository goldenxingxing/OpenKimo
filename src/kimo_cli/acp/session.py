from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from contextlib import suppress
from contextvars import ContextVar

import acp
import streamingjson  # type: ignore[reportMissingTypeStubs]
from kaos import Kaos, reset_current_kaos, set_current_kaos
from kosong.chat_provider import APIStatusError, ChatProviderError

from kimo_cli.acp.convert import (
    acp_blocks_to_content_parts,
    display_block_to_acp_content,
    tool_result_to_acp_content,
)
from kimo_cli.acp.types import ACPContentBlock
from kimo_cli.app import KimoCLI
from kimo_cli.soul import LLMNotSet, LLMNotSupported, MaxStepsReached, RunCancelled
from kimo_cli.soul.kimosoul import KimoSoul
from kimo_cli.tools import extract_key_argument
from kimo_cli.utils.logging import logger
from kimo_cli.wire.file import WireFile
from kimo_cli.wire.types import (
    ApprovalRequest,
    ApprovalResponse,
    CompactionBegin,
    CompactionEnd,
    ContentPart,
    MCPLoadingBegin,
    MCPLoadingEnd,
    Notification,
    PlanDisplay,
    QuestionRequest,
    StatusUpdate,
    SteerInput,
    StepBegin,
    StepInterrupted,
    StepRetry,
    SubagentEvent,
    TextPart,
    ThinkPart,
    TodoDisplayBlock,
    ToolCall,
    ToolCallPart,
    ToolCallRequest,
    ToolResult,
    TurnBegin,
    TurnEnd,
    WireMessage,
)

_current_turn_id = ContextVar[str | None]("current_turn_id", default=None)


def get_current_acp_tool_call_id_or_none() -> str | None:
    """See `_ToolCallState.acp_tool_call_id`."""
    from kimo_cli.soul.toolset import get_current_tool_call_or_none

    turn_id = _current_turn_id.get()
    if turn_id is None:
        return None
    tool_call = get_current_tool_call_or_none()
    if tool_call is None:
        return None
    return f"{turn_id}/{tool_call.id}"


class _ToolCallState:
    """Manages the state of a single tool call for streaming updates."""

    def __init__(self, tool_call: ToolCall):
        self.tool_call = tool_call
        self.args = tool_call.function.arguments or ""
        self.lexer = streamingjson.Lexer()
        if tool_call.function.arguments is not None:
            self.lexer.append_string(tool_call.function.arguments)

    @property
    def acp_tool_call_id(self) -> str:
        # When the user rejected or cancelled a tool call, the step result may not
        # be appended to the context. In this case, future step may emit tool call
        # with the same tool call ID (on the LLM side). To avoid confusion of the
        # ACP client, we ensure the uniqueness by prefixing with the turn ID.
        turn_id = _current_turn_id.get()
        assert turn_id is not None
        return f"{turn_id}/{self.tool_call.id}"

    def append_args_part(self, args_part: str) -> None:
        """Append a new arguments part to the accumulated args and lexer."""
        self.args += args_part
        self.lexer.append_string(args_part)

    def get_title(self) -> str:
        """Get the current title with subtitle if available."""
        tool_name = self.tool_call.function.name
        subtitle = extract_key_argument(self.lexer, tool_name)
        if subtitle:
            return f"{tool_name}: {subtitle}"
        return tool_name


class _TurnState:
    def __init__(self):
        self.id = str(uuid.uuid4())
        """Unique ID for the turn."""
        self.tool_calls: dict[str, _ToolCallState] = {}
        """Map of tool call ID (LLM-side ID) to tool call state."""
        self.last_tool_call: _ToolCallState | None = None
        self.cancel_event = asyncio.Event()


class ACPSession:
    #: What the agent is told when it is woken by a completed background task.
    #: Same wording the wire server and the interactive shell use, so all entry
    #: points read the same way in a transcript.
    _FOLLOWUP_PROMPT = (
        "<system-reminder>Background tasks completed while you were idle.</system-reminder>"
    )

    #: Give up auto-following-up after this many consecutive failures, rather
    #: than retrying a broken provider on every completion.
    _MAX_FOLLOWUP_FAILURES = 3

    def __init__(
        self,
        id: str,
        cli: KimoCLI,
        acp_conn: acp.Client,
        kaos: Kaos | None = None,
    ) -> None:
        self._id = id
        self._cli = cli
        self._conn = acp_conn
        self._kaos = kaos
        self._turn_state: _TurnState | None = None
        self._closed = False

        # Background follow-up: armed only after the user has driven a turn on
        # this session, so loading a session with old notifications pending
        # does not start talking on its own.
        self._followup_armed = False
        self._followup_failures = 0
        self._followup_disabled = False
        self._followup_task: asyncio.Task[None] | None = None
        self._maybe_start_background_followup()

    @property
    def id(self) -> str:
        """The ID of the ACP session."""
        return self._id

    @property
    def cli(self) -> KimoCLI:
        """The OpenKimo instance bound to this ACP session."""
        return self._cli

    async def close(self) -> None:
        """Stop session-owned background work and release CLI resources."""
        if self._closed:
            return
        self._closed = True
        followup_task = self._followup_task
        self._followup_task = None
        if followup_task is not None and not followup_task.done():
            followup_task.cancel()
        try:
            if followup_task is not None:
                with suppress(asyncio.CancelledError):
                    await followup_task
        except Exception as exc:
            logger.warning("Background follow-up task failed: {error}", error=exc)
        finally:
            await self._cli.close()

    def _is_oauth_session(self) -> bool:
        """Return True if the current session uses OAuth-based authentication."""
        try:
            llm = self._cli.soul.runtime.llm
            return llm is not None and getattr(llm.provider_config, "oauth", None) is not None
        except AttributeError:
            return False

    async def prompt(self, prompt: list[ACPContentBlock]) -> acp.PromptResponse:
        if self._turn_state is not None:
            # A follow-up turn can be in flight. Overwriting the state would
            # swap the cancel event and turn id out from under it and trip the
            # asserts in the _send_* helpers.
            raise acp.RequestError.internal_error(
                {"error": "A prompt is already in progress for this session"}
            )
        user_input = acp_blocks_to_content_parts(prompt)
        self._turn_state = _TurnState()
        token = _current_turn_id.set(self._turn_state.id)
        kaos_token = set_current_kaos(self._kaos) if self._kaos is not None else None
        self._followup_armed = True
        # A user turn re-arms auto-follow-up after a run of failures; without
        # this the session stays disabled for its whole lifetime.
        self._followup_failures = 0
        self._followup_disabled = False
        try:
            await self._pump_turn(self._cli.run(user_input, self._turn_state.cancel_event))
        except LLMNotSet as e:
            logger.exception("LLM not set:")
            raise acp.RequestError.auth_required() from e
        except LLMNotSupported as e:
            logger.exception("LLM not supported:")
            raise acp.RequestError.internal_error({"error": str(e)}) from e
        except APIStatusError as e:
            if e.status_code == 401 and self._is_oauth_session():
                logger.warning("Authentication failed (401), prompting re-login")
                raise acp.RequestError.auth_required() from e
            logger.exception("LLM API status error:")
            raise acp.RequestError.internal_error({"error": str(e)}) from e
        except ChatProviderError as e:
            logger.exception("LLM provider error:")
            raise acp.RequestError.internal_error({"error": str(e)}) from e
        except MaxStepsReached as e:
            logger.warning("Max steps reached: {n_steps}", n_steps=e.n_steps)
            return acp.PromptResponse(stop_reason="max_turn_requests")
        except RunCancelled:
            logger.info("Prompt cancelled by user")
            return acp.PromptResponse(stop_reason="cancelled")
        except Exception as e:
            logger.exception("Unexpected error during prompt:")
            raise acp.RequestError.internal_error({"error": str(e)}) from e
        finally:
            self._turn_state = None
            if kaos_token is not None:
                reset_current_kaos(kaos_token)
            _current_turn_id.reset(token)
        return acp.PromptResponse(stop_reason="end_turn")

    async def _pump_turn(self, msgs: AsyncGenerator[WireMessage, None]) -> None:
        """Forward one turn's wire messages to the client.

        Shared by `prompt` and the background follow-up turn so both stream
        exactly the same way.
        """
        async for msg in msgs:
            match msg:
                case TurnBegin():
                    pass
                case SteerInput():
                    pass
                case TurnEnd():
                    pass
                case StepBegin():
                    pass
                case StepInterrupted():
                    break
                case StepRetry():
                    pass
                case CompactionBegin():
                    pass
                case CompactionEnd():
                    pass
                case MCPLoadingBegin():
                    pass
                case MCPLoadingEnd():
                    pass
                case StatusUpdate():
                    pass
                case Notification():
                    await self._send_notification(msg)
                case ThinkPart(think=think):
                    await self._send_thinking(think)
                case TextPart(text=text):
                    await self._send_text(text)
                case ContentPart():
                    logger.warning("Unsupported content part: {part}", part=msg)
                    await self._send_text(f"[{msg.__class__.__name__}]")
                case ToolCall():
                    await self._send_tool_call(msg)
                case ToolCallPart():
                    await self._send_tool_call_part(msg)
                case ToolResult():
                    await self._send_tool_result(msg)
                case ApprovalResponse():
                    pass
                case SubagentEvent():
                    pass
                case PlanDisplay():
                    pass
                case ApprovalRequest():
                    await self._handle_approval_request(msg)
                case ToolCallRequest():
                    logger.warning("Unexpected ToolCallRequest in ACP session: {msg}", msg=msg)
                case QuestionRequest():
                    logger.warning(
                        "QuestionRequest is unsupported in ACP session; resolving empty answer."
                    )
                    msg.resolve({})
                case _:
                    pass

    async def replay_history(self, wire_file: WireFile) -> None:
        """Replay persisted wire history to an ACP client during session/load."""
        token = _current_turn_id.set(None)
        try:
            async for record in wire_file.iter_records():
                wire_msg = record.to_wire_message()
                match wire_msg:
                    case TurnBegin(user_input=user_input) | SteerInput(user_input=user_input):
                        self._turn_state = _TurnState()
                        _current_turn_id.set(self._turn_state.id)
                        await self._send_user_input(user_input)
                    case TurnEnd() | StepInterrupted():
                        self._turn_state = None
                        _current_turn_id.set(None)
                    case StepBegin():
                        pass
                    case CompactionBegin() | CompactionEnd():
                        pass
                    case MCPLoadingBegin() | MCPLoadingEnd():
                        pass
                    case StatusUpdate():
                        pass
                    case Notification():
                        await self._send_notification(wire_msg)
                    case ThinkPart(think=think):
                        await self._send_thinking(think)
                    case TextPart(text=text):
                        await self._send_text(text)
                    case ContentPart():
                        await self._send_text(f"[{wire_msg.__class__.__name__}]")
                    case ToolCall():
                        self._ensure_turn_state()
                        await self._send_tool_call(wire_msg)
                    case ToolCallPart():
                        if self._turn_state is not None:
                            await self._send_tool_call_part(wire_msg)
                    case ToolResult():
                        if self._turn_state is not None:
                            await self._send_tool_result(wire_msg)
                    case PlanDisplay():
                        pass
                    case ApprovalResponse() | SubagentEvent():
                        pass
                    case ApprovalRequest() | ToolCallRequest() | QuestionRequest():
                        pass
                    case _:
                        pass
        except Exception:
            logger.exception(
                "Failed to replay ACP session history from {file}:", file=wire_file.path
            )
            await self.close()
            raise
        finally:
            self._turn_state = None
            _current_turn_id.reset(token)

    async def cancel(self) -> None:
        if self._turn_state is None:
            logger.warning("Cancel requested but no prompt is running")
            return

        self._turn_state.cancel_event.set()
        # Disarm auto-follow-up so a later background completion does not spin
        # up a fresh autonomous turn right after the user hit Stop (see
        # _background_followup_loop). The next user prompt re-arms it in
        # _pump_turn's setup path via _followup_armed assignment above.
        self._followup_armed = False
        # Kill any running background bash tasks — Stop means the whole
        # session stops, not just the current turn. Same rationale and
        # calling pattern as wire/server.py::_handle_cancel.
        soul = getattr(self._cli, "soul", None)
        if isinstance(soul, KimoSoul):
            killed = soul.runtime.background_tasks.kill_all_active(reason="user_stop")
            if killed:
                logger.info(
                    "Stop killed {n} background task(s): {ids}",
                    n=len(killed),
                    ids=killed,
                )

    def _maybe_start_background_followup(self) -> None:
        """Report background task results without waiting to be asked.

        The ACP client only runs a turn when it sends one, so a background task
        finishing between prompts had no way to speak up: the result sat unread
        until the user's next, possibly unrelated, prompt. The wire server and
        the interactive shell already wake the agent on completion; this loop
        gives ACP the same behavior.
        """
        soul = getattr(self._cli, "soul", None)
        if not isinstance(soul, KimoSoul):
            return
        try:
            config = soul.runtime.config.background
        except AttributeError:
            return
        if not config.auto_followup:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._followup_task = asyncio.create_task(self._background_followup_loop())

    async def _background_followup_loop(self) -> None:
        """Wake the agent when an idle background task completes."""
        soul = self._cli.soul
        assert isinstance(soul, KimoSoul)
        runtime = soul.runtime
        manager = runtime.background_tasks
        notifications = runtime.notifications
        coalesce_s = runtime.config.background.auto_followup_coalesce_ms / 1000
        # `completion_event` is only set by publish_terminal_notifications(),
        # which only runs inside reconcile() — and between prompts nothing else
        # drives reconcile(). Poll the task store so a task that finishes
        # unnoticed still surfaces; same driver the wire server uses.
        poll_s = max(runtime.config.background.wait_poll_interval_ms / 1000, 0.1)

        while True:
            try:
                async with asyncio.timeout(poll_s):
                    await manager.completion_event.wait()
            except TimeoutError:
                pass
            try:
                manager.completion_event.clear()
                manager.reconcile()

                # Let a burst settle: five subagents finishing together should
                # produce one report, not five turns.
                if coalesce_s:
                    await asyncio.sleep(coalesce_s)
                    manager.completion_event.clear()
                    manager.reconcile()
            except Exception:
                logger.exception("Background follow-up reconcile failed; retrying next poll")
                continue

            if not self._followup_armed:
                logger.debug("Background completion ignored: no user turn yet")
                continue
            if self._turn_state is not None:
                # A prompt turn is running and will read the notification itself.
                continue
            if not notifications.has_pending_for_sink("llm"):
                continue
            if self._followup_failures >= self._MAX_FOLLOWUP_FAILURES:
                # The pending notification is left for the next user turn to pick
                # up. Warn exactly once: this branch is re-entered on every poll
                # (500ms by default), so logging here would flood the log.
                if not self._followup_disabled:
                    self._followup_disabled = True
                    logger.warning(
                        "Background auto-follow-up disabled after {n} consecutive failures",
                        n=self._followup_failures,
                    )
                continue

            logger.info("Background task completed while idle; following up")
            try:
                await self._run_followup_turn()
                self._followup_failures = 0
                self._followup_disabled = False
            except asyncio.CancelledError:
                raise
            except Exception:
                self._followup_failures += 1
                logger.exception(
                    "Background auto-follow-up failed ({n}/{max})",
                    n=self._followup_failures,
                    max=self._MAX_FOLLOWUP_FAILURES,
                )

    async def _run_followup_turn(self) -> None:
        """Run one turn on the agent's behalf and stream it like a prompt."""
        self._turn_state = _TurnState()
        token = _current_turn_id.set(self._turn_state.id)
        kaos_token = set_current_kaos(self._kaos) if self._kaos is not None else None
        try:
            await self._pump_turn(
                self._cli.run(self._FOLLOWUP_PROMPT, self._turn_state.cancel_event)
            )
        except RunCancelled:
            logger.info("Background follow-up turn cancelled")
        finally:
            self._turn_state = None
            if kaos_token is not None:
                reset_current_kaos(kaos_token)
            _current_turn_id.reset(token)

    def _ensure_turn_state(self) -> None:
        if self._turn_state is None:
            self._turn_state = _TurnState()
            _current_turn_id.set(self._turn_state.id)

    async def _send_user_input(self, user_input: str | list[ContentPart]) -> None:
        if not self._id or not self._conn:
            return

        parts = [TextPart(text=user_input)] if isinstance(user_input, str) else user_input
        for part in parts:
            if isinstance(part, TextPart):
                content: ACPContentBlock = acp.schema.TextContentBlock(type="text", text=part.text)
            else:
                logger.warning("Unsupported replay user input part: {part}", part=part)
                content = acp.schema.TextContentBlock(
                    type="text", text=f"[{part.__class__.__name__}]"
                )
            await self._conn.session_update(
                session_id=self._id,
                update=acp.schema.UserMessageChunk(
                    content=content,
                    session_update="user_message_chunk",
                ),
            )

    async def _send_thinking(self, think: str):
        """Send thinking content to client."""
        if not self._id or not self._conn:
            return

        await self._conn.session_update(
            self._id,
            acp.schema.AgentThoughtChunk(
                content=acp.schema.TextContentBlock(type="text", text=think),
                session_update="agent_thought_chunk",
            ),
        )

    async def _send_text(self, text: str):
        """Send text chunk to client."""
        if not self._id or not self._conn:
            return

        await self._conn.session_update(
            session_id=self._id,
            update=acp.schema.AgentMessageChunk(
                content=acp.schema.TextContentBlock(type="text", text=text),
                session_update="agent_message_chunk",
            ),
        )

    async def _send_notification(self, notification: Notification):
        """Send a system notification to the client as a text chunk."""
        body = notification.body.strip()
        text = f"[Notification] {notification.title}"
        if body:
            text = f"{text}\n{body}"
        await self._send_text(text)

    async def _send_tool_call(self, tool_call: ToolCall):
        """Send tool call to client."""
        assert self._turn_state is not None
        if not self._id or not self._conn:
            return

        # Create and store tool call state
        state = _ToolCallState(tool_call)
        self._turn_state.tool_calls[tool_call.id] = state
        self._turn_state.last_tool_call = state

        await self._conn.session_update(
            session_id=self._id,
            update=acp.schema.ToolCallStart(
                session_update="tool_call",
                tool_call_id=state.acp_tool_call_id,
                title=state.get_title(),
                status="in_progress",
                content=[
                    acp.schema.ContentToolCallContent(
                        type="content",
                        content=acp.schema.TextContentBlock(type="text", text=state.args),
                    )
                ],
            ),
        )
        logger.debug("Sent tool call: {name}", name=tool_call.function.name)

    async def _send_tool_call_part(self, part: ToolCallPart):
        """Send tool call part (streaming arguments)."""
        assert self._turn_state is not None
        if (
            not self._id
            or not self._conn
            or not part.arguments_part
            or self._turn_state.last_tool_call is None
        ):
            return

        # Append new arguments part to the last tool call
        self._turn_state.last_tool_call.append_args_part(part.arguments_part)

        # Update the tool call with new content and title
        update = acp.schema.ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id=self._turn_state.last_tool_call.acp_tool_call_id,
            title=self._turn_state.last_tool_call.get_title(),
            status="in_progress",
            content=[
                acp.schema.ContentToolCallContent(
                    type="content",
                    content=acp.schema.TextContentBlock(
                        type="text", text=self._turn_state.last_tool_call.args
                    ),
                )
            ],
        )

        await self._conn.session_update(session_id=self._id, update=update)
        logger.debug("Sent tool call update: {delta}", delta=part.arguments_part[:50])

    async def _send_tool_result(self, result: ToolResult):
        """Send tool result to client."""
        assert self._turn_state is not None
        if not self._id or not self._conn:
            return

        tool_ret = result.return_value

        state = self._turn_state.tool_calls.pop(result.tool_call_id, None)
        if state is None:
            logger.warning("Tool call not found: {id}", id=result.tool_call_id)
            return

        update = acp.schema.ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id=state.acp_tool_call_id,
            status="failed" if tool_ret.is_error else "completed",
        )

        contents = tool_result_to_acp_content(tool_ret)
        if contents:
            update.content = contents

        await self._conn.session_update(session_id=self._id, update=update)
        logger.debug("Sent tool result: {id}", id=result.tool_call_id)

        for block in tool_ret.display:
            if isinstance(block, TodoDisplayBlock):
                await self._send_plan_update(block)

    async def _handle_approval_request(self, request: ApprovalRequest):
        """Handle approval request by sending permission request to client."""
        assert self._turn_state is not None
        if not self._id or not self._conn:
            logger.warning("No session ID, auto-rejecting approval request")
            request.resolve("reject")
            return

        state = self._turn_state.tool_calls.get(request.tool_call_id, None)
        if state is None:
            logger.warning("Tool call not found: {id}", id=request.tool_call_id)
            request.resolve("reject")
            return

        try:
            content: list[
                acp.schema.ContentToolCallContent
                | acp.schema.FileEditToolCallContent
                | acp.schema.TerminalToolCallContent
            ] = []
            if request.display:
                for block in request.display:
                    diff_content = display_block_to_acp_content(block)
                    if diff_content is not None:
                        content.append(diff_content)
            if not content:
                content.append(
                    acp.schema.ContentToolCallContent(
                        type="content",
                        content=acp.schema.TextContentBlock(
                            type="text",
                            text=f"Requesting approval to perform: {request.description}",
                        ),
                    )
                )

            # Send permission request and wait for response
            logger.debug("Requesting permission for action: {action}", action=request.action)
            response = await self._conn.request_permission(
                [
                    acp.schema.PermissionOption(
                        option_id="approve",
                        name="Approve once",
                        kind="allow_once",
                    ),
                    acp.schema.PermissionOption(
                        option_id="approve_for_session",
                        name="Approve for this session",
                        kind="allow_always",
                    ),
                    acp.schema.PermissionOption(
                        option_id="reject",
                        name="Reject",
                        kind="reject_once",
                    ),
                ],
                self._id,
                acp.schema.ToolCallUpdate(
                    tool_call_id=state.acp_tool_call_id,
                    title=state.get_title(),
                    content=content,
                ),
            )
            logger.debug("Received permission response: {response}", response=response)

            # Process the outcome
            if isinstance(response.outcome, acp.schema.AllowedOutcome):
                # selected
                option_id = response.outcome.option_id
                if option_id == "approve":
                    logger.debug("Permission granted for: {action}", action=request.action)
                    request.resolve("approve")
                elif option_id == "approve_for_session":
                    logger.debug("Permission granted for session: {action}", action=request.action)
                    request.resolve("approve_for_session")
                else:
                    logger.debug("Permission denied for: {action}", action=request.action)
                    request.resolve("reject")
            else:
                # cancelled
                logger.debug("Permission request cancelled for: {action}", action=request.action)
                request.resolve("reject")
        except Exception:
            logger.exception("Error handling approval request:")
            # On error, reject the request
            request.resolve("reject")

    async def _send_plan_update(self, block: TodoDisplayBlock) -> None:
        """Send todo list updates as ACP agent plan updates."""

        status_map: dict[str, acp.schema.PlanEntryStatus] = {
            "pending": "pending",
            "in progress": "in_progress",
            "in_progress": "in_progress",
            "done": "completed",
            "completed": "completed",
        }
        entries: list[acp.schema.PlanEntry] = [
            acp.schema.PlanEntry(
                content=todo.title,
                priority="medium",
                status=status_map.get(todo.status.lower(), "pending"),
            )
            for todo in block.items
            if todo.title
        ]

        if not entries:
            logger.warning("No valid todo items to send in plan update: {todos}", todos=block.items)
            return

        await self._conn.session_update(
            session_id=self._id,
            update=acp.schema.AgentPlanUpdate(session_update="plan", entries=entries),
        )
