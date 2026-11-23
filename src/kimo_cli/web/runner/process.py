"""Session process management for the OpenKimo web interface."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import mimetypes
import os
import re
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from kosong.message import ContentPart, ImageURLPart, TextPart
from PIL import Image
from PIL.Image import Image as PILImage
from pydantic import TypeAdapter, ValidationError
from starlette.websockets import WebSocket, WebSocketState

from kimo_cli import logger

try:
    from pillow_heif import register_heif_opener  # type: ignore[import-not-found]

    register_heif_opener()
except Exception:  # pragma: no cover - degraded mode if pillow-heif missing
    logger.debug("pillow-heif not available; uploaded HEIC/HEIF will fail to decode")
from kimo_cli.config import LLMModel, load_config
from kimo_cli.llm import ModelCapability, derive_model_capabilities
from kimo_cli.share import get_share_dir
from kimo_cli.utils.subprocess_env import get_clean_env
from kimo_cli.web.models import (
    SessionNoticeEvent,
    SessionNoticePayload,
    SessionState,
    SessionStatus,
)
from kimo_cli.web.runner.activity import ActivityHub, SessionActivity
from kimo_cli.web.runner.messages import new_session_status_message
from kimo_cli.web.store.sessions import load_session_by_id
from kimo_cli.wire.jsonrpc import (
    JSONRPCCancelMessage,
    JSONRPCErrorObject,
    JSONRPCErrorResponse,
    JSONRPCEventMessage,
    JSONRPCInitializeMessage,
    JSONRPCInMessage,
    JSONRPCInMessageAdapter,
    JSONRPCOutMessage,
    JSONRPCPromptMessage,
    JSONRPCRequestMessage,
    JSONRPCSuccessResponse,
)
from kimo_cli.wire.serde import deserialize_wire_message
from kimo_cli.wire.types import (
    ApprovalRequest,
    ApprovalResponse,
    QuestionRequest,
    StepBegin,
    StepInterrupted,
    StepRetry,
    SubagentEvent,
    ToolCall,
    ToolResult,
    TurnEnd,
)

JSONRPCOutMessageAdapter = TypeAdapter[JSONRPCOutMessage](JSONRPCOutMessage)


def worker_log_path() -> Path | None:
    """Path of the log file a crashing worker's traceback ends up in.

    The worker calls ``enable_logging``, which swaps its process-level ``fd=2``
    into loguru. So when it dies there is nothing on the stderr pipe we read
    here, and the whole traceback is in this file instead. Name it in the error
    rather than saying "check the logs", which does not say which ones.

    Returns ``None`` if the share directory cannot be resolved, so that
    reporting one failure never raises a second one.
    """
    try:
        return get_share_dir() / "logs" / "kimo.log"
    except OSError:
        return None


# How far behind one browser tab may fall before it is dropped. A client this
# many messages (or bytes) behind cannot catch up on the live stream anyway;
# reconnecting replays `wire.jsonl` from the start, which is both correct and
# cheaper than holding the backlog.
WS_SEND_QUEUE_MAX_MESSAGES = 8192
WS_SEND_QUEUE_MAX_BYTES = 64 * 1024 * 1024
# A single frame that has not made it onto the wire in this long means the peer
# has stopped reading and is not coming back.
WS_SEND_TIMEOUT_S = 60.0
# Closing a wedged socket writes a close frame, which can block for the same
# reason the data did. Never wait on it.
WS_CLOSE_TIMEOUT_S = 5.0
# A connection idle this long gets a lightweight ping frame. A browser tab
# that went through a network blip may hold a half-dead TCP socket: no close
# event fires on either side, and the next real event may be minutes away.
# The ping surfaces that within one interval — send stalls or errors mean the
# peer is gone and the connection is torn down, so the client reconnects and
# replays instead of sitting on a dead socket.
WS_IDLE_PING_INTERVAL_S = 30.0

# Auto-restart after an unexpected worker exit: give a user-triggered start
# (send_message auto-starts the worker) a moment to win first, never restart a
# worker that crashes almost immediately (that is a persistent startup failure,
# and a tight crash loop is worse than a clearly-marked error), and cap the
# number of automatic restarts within the window.
AUTO_RESTART_DELAY_S = 1.0
AUTO_RESTART_MIN_WORKER_LIFETIME_S = 10.0
AUTO_RESTART_WINDOW_S = 300.0
AUTO_RESTART_MAX_ATTEMPTS = 3

#: After a stop click the worker should unwind its turn quickly — kill the
#: shell it spawned, close the LLM stream, write the turn's wire events. Any
#: of that can wedge on a given platform (observed on Windows: the stop button
#: froze the UI for a long time). The user has said stop, so the session must
#: not stay busy past this deadline no matter what the worker is stuck in:
#: terminate it. That is safe — the session lives on disk (wire + context),
#: and the next prompt auto-starts a fresh worker.
CANCEL_WATCHDOG_S = 15.0


class _WebSocketChannel:
    """The outbound half of one attached WebSocket.

    Every message the session produces used to be written to each attached
    socket inline, from the task that reads the worker's stdout. A browser tab
    that stopped draining its socket therefore stopped the read loop, the
    worker's stdout pipe filled, and the worker blocked mid-turn — while
    `wire.jsonl` kept being written, so reloading the page showed a turn that
    had in fact finished. Giving each connection its own queue and writer task
    keeps that backpressure where it belongs: on that one connection.
    """

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.dead = False
        self._queue: deque[str] = deque()
        self._queued_bytes = 0
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._on_dead: Callable[[WebSocket], Awaitable[None]] | None = None
        self._ping_seq = 0

    def enqueue(self, message: str) -> bool:
        """Queue a message for this connection. Never blocks.

        Returns False if the client has fallen too far behind to be worth
        keeping, in which case the caller should detach it.
        """
        if self.dead:
            return False
        if (
            len(self._queue) >= WS_SEND_QUEUE_MAX_MESSAGES
            or self._queued_bytes >= WS_SEND_QUEUE_MAX_BYTES
        ):
            logger.warning(
                "WebSocket send queue overflow "
                f"({len(self._queue)} msgs / {self._queued_bytes} bytes); dropping client"
            )
            self.dead = True
            return False
        self._queue.append(message)
        self._queued_bytes += len(message)
        self._wakeup.set()
        return True

    def start_writing(self, on_dead: Callable[[WebSocket], Awaitable[None]]) -> None:
        """Leave replay mode and start draining the queue to the socket."""
        self._on_dead = on_dead
        if self._task is None and not self.dead:
            self._task = asyncio.create_task(self._write_loop())

    async def _write_loop(self) -> None:
        try:
            while True:
                if self._queue:
                    message = self._queue.popleft()
                    self._queued_bytes -= len(message)
                else:
                    self._wakeup.clear()
                    if not self._queue:
                        try:
                            await asyncio.wait_for(self._wakeup.wait(), WS_IDLE_PING_INTERVAL_S)
                            continue
                        except TimeoutError:
                            # Idle past the ping interval: probe the
                            # connection. A distinct seq keeps every frame
                            # unique so clients' duplicate-frame detection
                            # never mistakes a ping for a re-delivery.
                            self._ping_seq += 1
                            message = (
                                '{"jsonrpc":"2.0","method":"ping",'
                                f'"params":{{"seq":{self._ping_seq}}}}}'
                            )
                    else:
                        continue
                if self.ws.client_state != WebSocketState.CONNECTED:
                    break
                try:
                    await asyncio.wait_for(self.ws.send_text(message), WS_SEND_TIMEOUT_S)
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    logger.warning(
                        f"websocket send stalled for {WS_SEND_TIMEOUT_S:.0f}s; dropping client"
                    )
                    break
                except Exception as e:
                    logger.warning(f"websocket failed: {e.__class__.__name__} {e}")
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"websocket writer crashed: {e.__class__.__name__} {e}")

        self.dead = True
        self._queue.clear()
        self._queued_bytes = 0
        if self._on_dead is not None:
            await self._on_dead(self.ws)

    async def shutdown(self) -> None:
        """Stop the writer task and discard anything still queued."""
        self.dead = True
        self._on_dead = None
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._queue.clear()
        self._queued_bytes = 0


class SessionProcess:
    """Manages a single session's KimoCLI subprocess.

    Handles:
    - Starting/stopping the subprocess
    - Reading from stdout (wire messages from KimoCLI)
    - Writing to stdin (user input to KimoCLI)
    - Broadcasting messages to connected WebSockets

    Concurrency model:
    - `SessionProcess` is the long-lived container for a `session_id`.
      It may outlive worker restarts.
    - Liveness vs busy are separate:
      - `is_alive` / `is_running`: worker subprocess exists and has not exited.
      - `is_busy`: there is at least one in-flight prompt id.
    - WebSocket fanout supports "join while running":
      - New clients replay `wire.jsonl` history first.
      - Live messages during replay are buffered per-WS and flushed afterwards.

    Locks:
    - `_lock` guards worker lifecycle and busy state.
    - `_ws_lock` guards WebSocket state.
    """

    def __init__(
        self,
        session_id: UUID,
        *,
        on_activity: Callable[[SessionActivity], None] | None = None,
        on_retry: Callable[[SessionProcess], None] | None = None,
    ) -> None:
        """Initialize a session process."""
        self.session_id = session_id
        self._in_flight_prompt_ids: set[str] = set()
        #: Approvals and questions sent to the client and not yet answered:
        #: JSON-RPC request id -> the request's own id, which is what an
        #: ``ApprovalRequestResolved`` event names.
        self._awaiting_requests: dict[str, str] = {}
        self._on_activity = on_activity
        self._on_retry = on_retry
        #: Between a failed model call and the next sign of progress.
        self._retrying = False
        self._published_activity: tuple[SessionState, bool] | None = None
        #: Its own counter: whether the session waits on a person changes
        #: without the status changing, and each change must still be newer.
        self._activity_seq = 0
        self._status_seq = 0
        self._worker_id: str | None = None
        self._status = SessionStatus(
            session_id=self.session_id,
            state="stopped",
            seq=self._status_seq,
            worker_id=self._worker_id,
            reason=None,
            detail=None,
            updated_at=datetime.now(UTC),
        )
        self._process: asyncio.subprocess.Process | None = None
        self._worker_started_at: float | None = None
        self._auto_restart_history: list[float] = []
        self._channels: dict[WebSocket, _WebSocketChannel] = {}
        self._websocket_count = 0
        self._closing_tasks: set[asyncio.Task[None]] = set()
        self._read_task: asyncio.Task[None] | None = None
        self._expecting_exit = False
        self._lock = asyncio.Lock()
        # Serializes prompt registration with the final busy check and worker
        # replacement. Kept separate from `_lock` so subclass start/stop hooks
        # (notably container cleanup) remain polymorphic and deadlock-free.
        self._prompt_lock = asyncio.Lock()
        # Incremented synchronously by deliberate stops and worker replacements.
        # Delayed restarts capture it and revalidate under `_lock` before they
        # are allowed to touch a worker.
        self._lifecycle_generation = 0
        self._ws_lock = asyncio.Lock()
        self._sent_files: set[str] = set()
        #: (names, .sent marker) for the message being delivered, so a failed
        #: stdin write can roll those "already sent" markers back.
        self._pending_sent: tuple[set[str], Path] | None = None
        self._deferred_restart_reason: str | None = None
        #: The client's last ``initialize``, replayed to a worker that replaces
        #: this one. See :meth:`_replay_initialize`.
        self._last_initialize: str | None = None
        self._deferred_restart_task: asyncio.Task[None] | None = None
        #: Armed when a cancel is forwarded; guarantees the turn really ends.
        self._cancel_watchdog_task: asyncio.Task[None] | None = None

    @property
    def is_alive(self) -> bool:
        """Whether the worker subprocess exists and has not exited."""
        process = self._process
        return process is not None and process.returncode is None

    @property
    def is_running(self) -> bool:
        """Backward-compatible name: indicates worker liveness."""
        return self.is_alive

    @property
    def is_busy(self) -> bool:
        """Whether the session is currently processing a prompt."""
        return len(self._in_flight_prompt_ids) > 0

    def clear_in_flight(self) -> None:
        """Clear stale in-flight prompt IDs (e.g. after an error)."""
        self._in_flight_prompt_ids.clear()

    @property
    def status(self) -> SessionStatus:
        """Current runtime status snapshot."""
        return self._status

    @property
    def websocket_count(self) -> int:
        """Get the number of connected WebSockets."""
        return self._websocket_count

    async def send_status_snapshot(self, ws: WebSocket) -> None:
        """Send the current status snapshot to a specific WebSocket.

        Goes through that connection's queue so it cannot overtake the live
        messages flushed when replay ended, and so a stalled peer cannot block
        the caller.
        """
        await self._send_to(ws, new_session_status_message(self._status).model_dump_json())

    def _build_status(
        self,
        state: SessionState,
        reason: str | None,
        detail: str | None,
    ) -> SessionStatus | None:
        """Build a new status object if different from current."""
        current = self._status
        if (
            current.state == state
            and current.reason == reason
            and current.detail == detail
            and current.worker_id == self._worker_id
        ):
            return None
        self._status_seq += 1
        status = SessionStatus(
            session_id=self.session_id,
            state=state,
            seq=self._status_seq,
            worker_id=self._worker_id,
            reason=reason,
            detail=detail,
            updated_at=datetime.now(UTC),
        )
        self._status = status
        return status

    async def _emit_status(
        self,
        state: SessionState,
        *,
        reason: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Emit a status update if different from current."""
        status = self._build_status(state, reason, detail)
        if status is None:
            return
        if state != "busy":
            # A turn that ended took its open requests with it.
            self._awaiting_requests.clear()
            self._retrying = False
        await self._broadcast(new_session_status_message(status).model_dump_json())
        self._publish_activity()

    @property
    def activity(self) -> SessionActivity:
        return SessionActivity(
            session_id=self.session_id,
            state=self._status.state,
            awaiting_input=bool(self._awaiting_requests),
            seq=self._activity_seq,
            updated_at=self._status.updated_at,
        )

    def _publish_activity(self) -> None:
        """Tell subscribers when the state, or whether it waits on a person, changed."""
        if self._on_activity is None:
            return
        current = (self._status.state, bool(self._awaiting_requests))
        if current == self._published_activity:
            return
        self._published_activity = current
        self._activity_seq += 1
        try:
            self._on_activity(self.activity)
        except Exception:
            logger.exception("Publishing activity for session {id} failed", id=self.session_id)

    def _note_answered(self, rpc_id: str) -> None:
        """The client answered an approval or a question."""
        if self._awaiting_requests.pop(rpc_id, None) is not None:
            self._publish_activity()

    @property
    def is_retrying(self) -> bool:
        """A model call failed and the turn is waiting to try it again."""
        return self._retrying

    def _track_retrying(self, message: JSONRPCOutMessage) -> None:
        if not isinstance(message, JSONRPCEventMessage):
            return
        event = message.params
        # A foreground subagent retries inside the turn that is waiting on it,
        # so its failed model calls count exactly as the turn's own would.
        while isinstance(event, SubagentEvent):
            event = event.event
        if isinstance(event, StepRetry):
            self._retrying = True
            if self._on_retry is not None:
                try:
                    self._on_retry(self)
                except Exception:
                    logger.exception("Retry hook for session {id} failed", id=self.session_id)
        elif isinstance(
            event,
            StepBegin | StepInterrupted | TurnEnd | ToolCall | ToolResult | ContentPart,
        ):
            self._retrying = False

    async def cancel_turn(self, reason: str) -> None:
        """Stop the running turn exactly as the stop button does."""
        if not self.is_busy:
            return
        logger.info("Cancelling turn in session {id}: {reason}", id=self.session_id, reason=reason)
        await self.send_message(
            json.dumps({"jsonrpc": "2.0", "method": "cancel", "id": uuid4().hex})
        )

    def _arm_cancel_watchdog(self, prompt_ids: set[str]) -> None:
        """After a stop click, guarantee the session stops being busy.

        The worker answers a cancel by unwinding the turn, and the unwind is
        expected to finish in far less than ``CANCEL_WATCHDOG_S``. When it
        does not — the turn's prompt response never arrives, ``is_busy``
        stays true, and the UI sits on its stopping state — the watchdog
        terminates the worker. ``prompt_ids`` is the set of in-flight prompts
        at cancel time: once those are answered the cancelled turn is over
        (whether a new one started), and the watchdog stands down.
        """
        if self._cancel_watchdog_task is not None:
            self._cancel_watchdog_task.cancel()
        self._cancel_watchdog_task = asyncio.create_task(self._cancel_watchdog(prompt_ids))

    async def _cancel_watchdog(self, prompt_ids: set[str]) -> None:
        deadline = time.monotonic() + CANCEL_WATCHDOG_S
        while True:
            if not (prompt_ids & self._in_flight_prompt_ids):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.25, remaining))
        logger.warning(
            "Session {sid} is still busy {seconds:.0f}s after a cancel was "
            "forwarded; terminating the worker to unblock the stop. The "
            "unwind it never finished is logged above this line.",
            sid=self.session_id,
            seconds=CANCEL_WATCHDOG_S,
        )
        await self.stop_worker(reason="cancel_watchdog")

    def _track_awaiting(self, message: JSONRPCOutMessage) -> None:
        match message:
            case JSONRPCRequestMessage(params=ApprovalRequest() | QuestionRequest() as request):
                # Only track live ones. History replayed into a fresh client
                # after a worker restart also carries requests, but their turn
                # is already dead — tracking them would leave the session
                # pretending to wait on input nobody can answer or cancel
                # (the resurrected dialog that outlives its turn).
                if self._status.state == "busy":
                    self._awaiting_requests[message.id] = request.id
            case JSONRPCEventMessage(params=ApprovalResponse() as resolved):
                # Resolved by another client, or on the worker's side.
                for rpc_id, request_id in tuple(self._awaiting_requests.items()):
                    if request_id == resolved.request_id:
                        del self._awaiting_requests[rpc_id]
            case _:
                return
        self._publish_activity()

    async def start(
        self,
        *,
        reason: str | None = None,
        detail: str | None = None,
        restart_started_at: float | None = None,
    ) -> None:
        """Start the KimoCLI subprocess."""
        async with self._lock:
            await self._start_locked(
                reason=reason,
                detail=detail,
                restart_started_at=restart_started_at,
            )

    async def _start_locked(
        self,
        *,
        reason: str | None = None,
        detail: str | None = None,
        restart_started_at: float | None = None,
    ) -> None:
        """Start the worker while `_lock` serializes prompt registration."""
        if self.is_alive:
            if self._read_task is None or self._read_task.done():
                self._read_task = asyncio.create_task(self._read_loop())
            return

        self._lifecycle_generation += 1
        self._in_flight_prompt_ids.clear()
        self._expecting_exit = False
        self._worker_id = str(uuid4())

        # 16MB buffer for large messages (e.g., base64-encoded images)
        STREAM_LIMIT = 16 * 1024 * 1024

        if getattr(sys, "frozen", False):
            worker_cmd = [sys.executable, "__web-worker", str(self.session_id)]
        else:
            worker_cmd = [
                sys.executable,
                "-m",
                "kimo_cli.web.runner.worker",
                str(self.session_id),
            ]

        self._process = await asyncio.create_subprocess_exec(
            *worker_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
            env=get_clean_env(),
        )
        self._worker_started_at = time.monotonic()

        self._read_task = asyncio.create_task(self._read_loop())
        await self._replay_initialize()
        if restart_started_at is not None:
            elapsed_ms = int((time.perf_counter() - restart_started_at) * 1000)
            detail = f"restart_ms={elapsed_ms}"
            await self._emit_status("idle", reason=reason or "start", detail=detail)
            await self._emit_restart_notice(reason=reason, restart_ms=elapsed_ms)
        else:
            await self._emit_status("idle", reason=reason or "start", detail=None)

    async def stop(self) -> None:
        """Stop the session: terminate worker and close all WebSockets."""
        await self.stop_worker(reason="stop")
        await self._close_all_websockets()

    async def stop_worker(
        self,
        *,
        reason: str | None = None,
        emit_status: bool = True,
    ) -> None:
        """Stop only the worker subprocess, keeping WebSockets connected."""
        # Invalidate sleepers before the first await. `_expecting_exit` alone
        # was only a transient flag: a delayed auto-restart could wake after it
        # had been cleared and resurrect the deliberately stopped worker.
        if reason != "restart":
            self._lifecycle_generation += 1
            self._deferred_restart_reason = None
        async with self._lock:
            await self._stop_worker_locked(reason=reason, emit_status=emit_status)

    async def _stop_worker_locked(
        self,
        *,
        reason: str | None = None,
        emit_status: bool = True,
    ) -> None:
        """Stop the current worker; the caller holds `_lock`."""
        # A deliberate stop supersedes the watchdog. Skip the current task
        # though: the watchdog itself escalates through stop_worker.
        watchdog = self._cancel_watchdog_task
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()
        self._cancel_watchdog_task = None
        self._expecting_exit = True
        if self._process is not None:
            if self._process.returncode is None:
                self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10.0)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
            self._process = None

        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
            self._read_task = None

        self._in_flight_prompt_ids.clear()
        self._worker_id = None
        self._expecting_exit = False
        if emit_status:
            await self._emit_status("stopped", reason=reason or "stop")

    def defer_restart(self, reason: str) -> None:
        """Owe this session a restart, to be paid when it stops being busy.

        A config change must not interrupt a prompt, so a busy session is
        skipped -- but skipping alone left it on the old config until something
        else happened to restart it, which for a session nobody restarts is
        forever. The debt is recorded here and settled at the next busy -> idle
        transition.
        """
        self._deferred_restart_reason = reason

    def _settle_deferred_restart(self) -> None:
        """Pay a deferred restart now that the session has gone idle.

        Runs as its own task: the caller is inside the worker's read loop, and
        `stop_worker` cancels and awaits that very task.
        """
        if self._deferred_restart_reason is None or self.is_busy or not self.is_alive:
            return
        if self._deferred_restart_task is not None and not self._deferred_restart_task.done():
            return
        task = asyncio.create_task(self._apply_deferred_restart())
        self._deferred_restart_task = task
        task.add_done_callback(lambda _: None)

    async def _apply_deferred_restart(self) -> None:
        reason = self._deferred_restart_reason
        if reason is None:
            return
        # A new prompt may have arrived between the transition and this task.
        # The debt stays on the books for the next time it goes idle.
        if self.is_busy or not self.is_alive:
            return
        generation = self._lifecycle_generation
        try:
            await self.restart_worker(reason=reason, expected_generation=generation)
        except Exception:
            logger.exception(f"Deferred restart failed for session {self.session_id}")

    async def restart_worker(
        self,
        *,
        reason: str | None = None,
        force: bool = False,
        expected_generation: int | None = None,
    ) -> None:
        """Restart without racing prompt registration or a deliberate stop."""
        started_at = time.perf_counter()
        await self._emit_status("restarting", reason=reason or "restart")
        async with self._prompt_lock:
            if (
                expected_generation is not None
                and expected_generation != self._lifecycle_generation
            ):
                return
            if self.is_busy and not force:
                # The busy check and termination share `_prompt_lock` with
                # prompt registration. A prompt that arrived while the status
                # broadcast awaited is deferred around, never killed after a
                # stale idle observation.
                self._deferred_restart_reason = reason or "restart"
                await self._emit_status("busy", reason="prompt")
                return

            # Any restart loads the current config, so it settles the debt
            # whatever asked for it.
            self._deferred_restart_reason = None
            await self.stop_worker(reason="restart", emit_status=False)
            if (
                expected_generation is not None
                and expected_generation != self._lifecycle_generation
            ):
                # A deliberate stop slipped between the two lifecycle calls.
                # It owns the stopped state; do not start a replacement.
                return
            await self.start(
                reason=reason or "restart",
                restart_started_at=started_at,
            )

    def _schedule_auto_restart(self) -> None:
        """Recover from an unexpected worker exit by restarting it, unless the
        exit looks like a persistent crash (restart would just crash-loop)."""
        generation = self._lifecycle_generation
        task = asyncio.create_task(self._auto_restart_after_unexpected_exit(generation))
        self._closing_tasks.add(task)
        task.add_done_callback(self._closing_tasks.discard)

    async def _auto_restart_after_unexpected_exit(self, generation: int) -> None:
        await asyncio.sleep(AUTO_RESTART_DELAY_S)
        # A user-triggered start (send_message auto-starts the worker) or a
        # deliberate stop may have happened while we waited — never kill a
        # healthy fresh worker. The generation is checked again under `_lock`
        # by restart_worker immediately before termination and again before start.
        if generation != self._lifecycle_generation or self.is_alive or self._expecting_exit:
            return
        now = time.monotonic()
        if (
            self._worker_started_at is not None
            and now - self._worker_started_at < AUTO_RESTART_MIN_WORKER_LIFETIME_S
        ):
            logger.warning(
                f"Session {self.session_id}: worker exited after "
                f"{now - self._worker_started_at:.1f}s; not auto-restarting "
                "(looks like a persistent crash)"
            )
            return
        self._auto_restart_history = [
            t for t in self._auto_restart_history if now - t < AUTO_RESTART_WINDOW_S
        ]
        if len(self._auto_restart_history) >= AUTO_RESTART_MAX_ATTEMPTS:
            logger.warning(
                f"Session {self.session_id}: {len(self._auto_restart_history)} "
                "auto-restarts within "
                f"{AUTO_RESTART_WINDOW_S:.0f}s; giving up (session left in error state)"
            )
            return
        self._auto_restart_history.append(now)
        try:
            await self.restart_worker(reason="auto_recover", expected_generation=generation)
        except Exception:
            logger.exception(f"Auto-restart failed for session {self.session_id}")

    async def _replay_initialize(self) -> None:
        """Tell a replacement worker what the attached client can do.

        A restart swaps the subprocess while the WebSockets stay attached, so
        the browser — which only sends ``initialize`` when its socket opens —
        never tells the new worker anything. It came up with the defaults
        instead: no question support, so every AskUserQuestion after a restart
        failed with "the connected client does not support interactive
        questions"; no plan mode; and no external tools, hooks or client info.

        Sent before the idle status, so it is on the worker's stdin ahead of
        any prompt the client sends on seeing it.
        """
        message = self._last_initialize
        process = self._process
        if message is None or process is None or process.stdin is None:
            return
        try:
            process.stdin.write((message + "\n").encode("utf-8"))
            await process.stdin.drain()
        except Exception:
            logger.exception(f"Failed to replay initialize for session {self.session_id}")

    async def _emit_restart_notice(self, *, reason: str | None, restart_ms: int) -> None:
        """Emit a restart notice to all WebSockets."""
        label = "Session restarted"
        if reason == "config_update":
            label = "Session restarted due to config update"
        payload = SessionNoticePayload(
            text=f"{label} · {restart_ms}ms",
            kind="restart",
            reason=reason,
            restart_ms=restart_ms,
        )
        event = SessionNoticeEvent(payload=payload)
        await self._broadcast(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": event.model_dump(mode="json"),
                },
                ensure_ascii=False,
            )
        )

    async def _read_loop(self) -> None:
        """Read messages from subprocess stdout and broadcast to WebSockets."""
        assert self._process is not None
        assert self._process.stdout is not None
        assert self._process.stderr is not None

        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    if self._process.stdout.at_eof():
                        if self._expecting_exit:
                            break
                        stderr = await self._process.stderr.read()
                        # On Windows (ProactorEventLoop), asyncio does not
                        # automatically set returncode when the pipe reaches EOF.
                        # Call wait() explicitly to ensure it is populated.
                        if self._process.returncode is None:
                            await self._process.wait()
                        if not stderr:
                            log_path = worker_log_path()
                            where = (
                                f"Its stderr is redirected into {log_path} —"
                                " the traceback is there."
                                if log_path is not None
                                else "Check logs for details."
                            )
                            err_msg = (
                                "Worker process exited unexpectedly"
                                f" (exit code {self._process.returncode}). "
                                f"{where}"
                            )
                            stderr = err_msg.encode()
                        # Clear in-flight IDs before broadcasting so that
                        # is_busy is already False when the frontend reacts
                        # to the error and sends a new prompt.
                        self._in_flight_prompt_ids.clear()
                        await self._broadcast(
                            JSONRPCErrorResponse(
                                id=str(uuid4()),
                                error=JSONRPCErrorObject(
                                    code=self._process.returncode or -1,
                                    message=stderr.decode("utf-8"),
                                ),
                            ).model_dump_json()
                        )
                        logger.warning(
                            f"Process exited with {self._process.returncode}: "
                            f"{stderr.decode('utf-8')}"
                        )
                        await self._emit_status(
                            "error",
                            reason="process_exit",
                            detail=stderr.decode("utf-8"),
                        )
                        self._schedule_auto_restart()
                        break
                    else:
                        continue

                await self._broadcast(line.decode("utf-8").rstrip("\n"))

                # Handle out message
                try:
                    msg = json.loads(line)
                    match msg.get("method"):
                        case "event":
                            msg["params"] = deserialize_wire_message(msg["params"])
                            await self._handle_out_message(JSONRPCEventMessage.model_validate(msg))
                        case "request":
                            msg["params"] = deserialize_wire_message(msg["params"])
                            await self._handle_out_message(
                                JSONRPCRequestMessage.model_validate(msg)
                            )
                        case _:
                            if msg.get("error"):
                                await self._handle_out_message(
                                    JSONRPCErrorResponse.model_validate(msg)
                                )
                            else:
                                await self._handle_out_message(
                                    JSONRPCSuccessResponse.model_validate(msg)
                                )
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSONRPC out message: {line}")
                except ValidationError:
                    # One malformed line must not kill the session: log it,
                    # skip it, and keep reading. A worker bug that emits an
                    # unwrapped wire message (see the bare-StatusUpdate
                    # regression) would otherwise end the turn for every
                    # attached client and mark the session "error".
                    logger.error(f"Malformed JSONRPC out message (skipped): {line}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Unexpected error in read loop: {e.__class__.__name__} {e}")
            self._in_flight_prompt_ids.clear()
            await self._emit_status("error", reason="read_loop_error", detail=str(e))
            self._schedule_auto_restart()

    async def _handle_out_message(self, message: JSONRPCOutMessage) -> None:
        """Handle outbound message from worker."""
        self._track_awaiting(message)
        self._track_retrying(message)
        match message:
            case JSONRPCSuccessResponse():
                if message.id in self._in_flight_prompt_ids:
                    self._in_flight_prompt_ids.remove(message.id)
                # Emit idle whenever the in-flight set drained and the last
                # broadcast state is still "busy" — not only on the exact
                # busy→idle edge. If a flip was ever missed (an id leaked out
                # of the set without the matching response, a restart raced a
                # completion), the old was_busy guard left the session stuck
                # broadcasting "busy" forever, and every frontend watching it
                # spun "thinking" with nothing running. _emit_status dedups
                # identical states, so the extra check is free when healthy.
                if not self.is_busy and self._status.state == "busy":
                    await self._emit_status("idle", reason="prompt_complete")
                    self._settle_deferred_restart()
            case JSONRPCErrorResponse():
                if message.id in self._in_flight_prompt_ids:
                    self._in_flight_prompt_ids.remove(message.id)
                if not self.is_busy and self._status.state == "busy":
                    await self._emit_status("idle", reason="prompt_error")
                    self._settle_deferred_restart()
            case _:
                return

    async def _encode_uploaded_files(
        self, inline: frozenset[str] = frozenset()
    ) -> tuple[list[ContentPart], dict[str, list[ContentPart]]]:
        """Encode this turn's uploaded files off the event loop."""
        # The whole body below is synchronous file I/O and base64 encoding.
        # Running it inline stalls every other session while a large upload
        # is being read.
        return await asyncio.to_thread(self._encode_uploaded_files_sync, inline)

    def _encode_uploaded_files_sync(
        self, inline: frozenset[str] = frozenset()
    ) -> tuple[list[ContentPart], dict[str, list[ContentPart]]]:
        """Encode this turn's uploaded files for the model.

        Returns the parts that go before the message — files the message does
        not place — and, per stored name, the parts of each file the message
        places with an ``<attachment file=…/>`` marker. A placed file's tags
        carry ``inline="1"`` so a replayed history can put it back in the text.
        """
        session = load_session_by_id(self.session_id)
        assert session is not None

        uploads_dir = session.kimo_cli_session.dir / "uploads"
        if not uploads_dir.exists():
            return [], {}

        # Load .sent marker left by fork to avoid re-sending inherited files.
        # The marker is kept (not deleted) so it survives process restarts.
        sent_marker = uploads_dir / ".sent"
        if sent_marker.exists():
            try:
                already_sent = json.loads(sent_marker.read_text(encoding="utf-8"))
                self._sent_files.update(already_sent)
            except Exception as e:
                logger.debug("Failed to read sent files marker: {err}", err=e)

        all_files = sorted(
            (f for f in uploads_dir.iterdir() if f.name != ".sent"),
            key=lambda x: x.name,
        )
        files = [f for f in all_files if f.name not in self._sent_files]

        if not files:
            return [], {}

        # Check model capabilities of the session's effective model
        # (per-session override, falling back to the global default).
        config = load_config()
        capabilities: set[ModelCapability] = set()
        effective_model_name = session.model or config.default_model
        if effective_model_name and effective_model_name in config.models:
            model_config = config.models[effective_model_name]
            capabilities = derive_model_capabilities(model_config)
        else:
            # Fallback: derive from env var when config file has no model entry
            env_model_name = os.environ.get("OPENAI_MODEL_NAME") or os.environ.get(
                "ANTHROPIC_MODEL_NAME"
            )
            if env_model_name:
                capabilities = derive_model_capabilities(
                    LLMModel(provider="", model=env_model_name, max_context_size=100_000)
                )
        is_vision = "image_in" in capabilities
        is_video_in = "video_in" in capabilities

        prefix: list[ContentPart] = []
        placed: dict[str, list[ContentPart]] = {}
        unplaced = [file for file in files if file.name not in inline]
        if unplaced:
            # Output file list summary
            file_list_lines = ["<uploaded_files>"]
            for idx, file in enumerate(unplaced, start=1):
                file_list_lines.append(f"{idx}. {file}")
            file_list_lines.append("</uploaded_files>")
            prefix.append(TextPart(text="\n".join(file_list_lines) + "\n\n"))

        for file in files:
            mime_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            is_placed = file.name in inline
            parts = self._file_parts(
                file,
                mime_type,
                inline=is_placed,
                is_vision=is_vision,
                is_video_in=is_video_in,
            )
            if is_placed:
                placed[file.name] = parts
            else:
                prefix.extend(parts)

        # Mark files as sent and persist to disk so state survives server
        # restarts. Remember what was marked so send_message can roll the
        # markers back if the write to the worker's stdin fails.
        for file in files:
            self._sent_files.add(file.name)
        self._pending_sent = ({file.name for file in files}, sent_marker)
        with contextlib.suppress(Exception):
            sent_marker.write_text(json.dumps(sorted(self._sent_files)), encoding="utf-8")
        return prefix, placed

    @staticmethod
    def _file_parts(
        file: Path,
        mime_type: str,
        *,
        inline: bool,
        is_vision: bool,
        is_video_in: bool,
    ) -> list[ContentPart]:
        """One file as the model sees it, before the message or in its place."""
        file_path = str(file)
        attrs = f'path="{file_path}" content_type="{mime_type}"' + (' inline="1"' if inline else "")
        gap = "" if inline else "\n\n"
        ext = file.suffix.lower()

        if is_vision and mime_type.startswith("image/"):
            try:
                content = file.read_bytes()
                with Image.open(io.BytesIO(content)) as img:
                    pil_img: PILImage = img
                    width, height = pil_img.size
                    max_side = max(width, height)
                    if max_side > 4096:
                        scale = 4096 / max_side
                        new_size = (int(width * scale), int(height * scale))
                        pil_img = pil_img.resize(  # pyright: ignore[reportUnknownMemberType]
                            new_size
                        )
                    buffer = io.BytesIO()
                    pil_img.save(buffer, format="PNG")
                    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                return [
                    TextPart(text=f"<image {attrs}>"),
                    ImageURLPart(
                        image_url=ImageURLPart.ImageURL(url=f"data:image/png;base64,{encoded}")
                    ),
                    TextPart(text=f"</image>{gap}"),
                ]
            except Exception:
                logger.exception("Failed to encode uploaded image {}", file_path)
        elif is_video_in and mime_type.startswith("video/"):
            # For video files, emit a <video> tag for frontend display but don't embed content.
            # The agent will use ReadMediaFile tool to read it, which handles video uploads
            # properly.
            return [TextPart(text=f"<video {attrs}>"), TextPart(text=f"</video>{gap}")]
        elif ext in _TEXT_EXTENSIONS or mime_type.startswith("text/"):
            try:
                text_content = file.read_bytes().decode("utf-8", errors="replace")
                return [
                    TextPart(text=f"<document {attrs}>"),
                    TextPart(text=text_content),
                    TextPart(text=f"</document>{gap}"),
                ]
            except Exception:
                # Skip files that fail to decode - don't block the upload
                pass
        # Anything else the model reads with its own tools. Before the message
        # it is named in the file list; in place it needs naming right there.
        return [TextPart(text=f"<file {attrs}/>")] if inline else []

    async def _handle_in_message(self, message: JSONRPCInMessage) -> str | None:
        """Handle inbound message to worker, encoding uploaded files."""
        match message:
            case JSONRPCPromptMessage():
                raw_input = message.params.user_input
                markers = (
                    list(_ATTACHMENT_MARKER.finditer(raw_input))
                    if isinstance(raw_input, str)
                    else []
                )
                prefix, placed = await self._encode_uploaded_files(
                    frozenset(marker.group(1) for marker in markers)
                )
                user_input: list[ContentPart] = list(prefix)
                # Special marker for file-only uploads
                if isinstance(raw_input, str):
                    if raw_input != "KIMO_FILE_UPLOAD_WITHOUT_MESSAGE":
                        # Each placed file goes where its marker stood; a marker
                        # for a file that is not (or no longer) uploaded is dropped.
                        position = 0
                        for marker in markers:
                            if marker.start() > position:
                                user_input.append(
                                    TextPart(text=raw_input[position : marker.start()])
                                )
                            user_input.extend(placed.get(marker.group(1), []))
                            position = marker.end()
                        if position < len(raw_input):
                            user_input.append(TextPart(text=raw_input[position:]))
                else:
                    user_input += raw_input
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "prompt",
                        "id": message.id,
                        "params": {
                            "user_input": [part.model_dump(mode="json") for part in user_input],
                        },
                    },
                    ensure_ascii=False,
                )
            case _:
                return None
        return None

    async def _broadcast(self, message: str) -> None:
        """Hand a message to every attached WebSocket. Never blocks on a client.

        This runs on the task that drains the worker's stdout, so it must
        return promptly no matter how slowly any browser tab is reading.
        Delivery is the job of each connection's own writer task.
        """
        async with self._ws_lock:
            channels = list(self._channels.values())

        for channel in channels:
            if not channel.enqueue(message):
                await self._drop_websocket(channel.ws, reason="send queue overflow")

    async def _send_to(self, ws: WebSocket, message: str) -> None:
        """Queue a message for one connection. Never blocks on the client."""
        async with self._ws_lock:
            channel = self._channels.get(ws)
        if channel is None:
            return
        if not channel.enqueue(message):
            await self._drop_websocket(ws, reason="send queue overflow")

    async def _drop_websocket(self, ws: WebSocket, *, reason: str) -> None:
        """Detach a connection that can no longer keep up, and close it.

        Detaching first is what matters: once the socket is out of
        ``_channels`` it can no longer hold anything up, whatever the close
        handshake does. The client reconnects and replays `wire.jsonl`, so
        nothing it missed is lost.
        """
        async with self._ws_lock:
            channel = self._channels.pop(ws, None)
            self._websocket_count = len(self._channels)
        if channel is None:
            return
        logger.warning(f"Dropping WebSocket ({reason}), remaining={self._websocket_count}")
        self._teardown(channel, code=1011, reason="Client fell behind")

    def _teardown(
        self,
        channel: _WebSocketChannel,
        *,
        code: int,
        reason: str,
    ) -> None:
        """Stop a detached connection's writer and close its socket, in the background.

        Both halves can block for the very reason the connection is being torn
        down: cancelling a writer means waiting for a send that may not be
        going anywhere, and the close handshake writes a frame of its own. The
        caller here may be the read loop, so it waits for neither — the socket
        is already out of ``_channels`` and can no longer hold anything up.
        """

        async def teardown() -> None:
            await channel.shutdown()
            if channel.ws.client_state != WebSocketState.CONNECTED:
                return
            # Already gone, or wedged: either way we are done with it.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    channel.ws.close(code=code, reason=reason), WS_CLOSE_TIMEOUT_S
                )

        task = asyncio.create_task(teardown())
        self._closing_tasks.add(task)
        task.add_done_callback(self._closing_tasks.discard)

    async def add_websocket_and_begin_replay(self, ws: WebSocket) -> None:
        """Atomically attach a WebSocket and enter replay mode for it."""
        async with self._ws_lock:
            if ws not in self._channels:
                self._channels[ws] = _WebSocketChannel(ws)
                self._websocket_count = len(self._channels)
        logger.debug(f"WebSocket added (replay mode), count={self._websocket_count}")

    async def end_replay(self, ws: WebSocket) -> None:
        """Start delivering live messages to a websocket after history replay.

        Anything broadcast during the replay is already queued in order, so the
        writer picks up exactly where the replayed history left off.
        """
        async with self._ws_lock:
            channel = self._channels.get(ws)
        if channel is None:
            return
        channel.start_writing(self._on_channel_dead)

    async def _on_channel_dead(self, ws: WebSocket) -> None:
        """A connection's writer gave up: forget it and close the socket."""
        async with self._ws_lock:
            existing = self._channels.get(ws)
            if existing is None or not existing.dead:
                return
            channel = self._channels.pop(ws, None)
            self._websocket_count = len(self._channels)
        if channel is None:
            return
        logger.debug(f"WebSocket writer ended, remaining={self._websocket_count}")
        self._teardown(channel, code=1011, reason="Send failed")

    async def _close_all_websockets(self) -> None:
        """Close all connected WebSockets."""
        async with self._ws_lock:
            channels = list(self._channels.values())
            self._channels.clear()
            self._websocket_count = 0

        async def close(channel: _WebSocketChannel) -> None:
            await channel.shutdown()
            if channel.ws.client_state != WebSocketState.CONNECTED:
                return
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    channel.ws.close(code=1001, reason="Session process exited"),
                    WS_CLOSE_TIMEOUT_S,
                )

        # Concurrently: one wedged peer must not make everyone else wait out
        # its close timeout too.
        await asyncio.gather(*(close(channel) for channel in channels))

    async def remove_websocket(self, ws: WebSocket) -> None:
        """Remove a WebSocket connection from this session."""
        async with self._ws_lock:
            channel = self._channels.pop(ws, None)
            self._websocket_count = len(self._channels)
        if channel is not None:
            logger.debug(f"WebSocket removed, count={self._websocket_count}")
            await channel.shutdown()

    async def apply_compaction_ratio(self, ratio: float) -> bool:
        """Push a new auto-compaction ratio into a live worker.

        Returns whether the worker was running and the message was written.
        A stopped worker needs nothing: it reads the persisted value from
        config.toml the next time it starts.
        """
        if not self.is_running:
            return False
        process = self._process
        if process is None or process.stdin is None:
            return False
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "set_compaction_ratio",
                "id": uuid4().hex,
                "params": {"ratio": ratio},
            },
            ensure_ascii=False,
        )
        try:
            process.stdin.write((payload + "\n").encode())
            await process.stdin.drain()
        except (OSError, RuntimeError):
            logger.warning(
                "Could not push the compaction ratio to session {sid}",
                sid=self.session_id,
            )
            return False
        return True

    def _rollback_pending_sent_files(self) -> None:
        """Let a failed send retry this turn's attachments."""
        if self._pending_sent is None:
            return
        names, marker = self._pending_sent
        self._sent_files.difference_update(names)
        with contextlib.suppress(Exception):
            marker.write_text(json.dumps(sorted(self._sent_files)), encoding="utf-8")
        self._pending_sent = None

    async def _rollback_failed_prompt(self, prompt_id: str) -> None:
        """Roll back one failed send without disturbing concurrent prompts."""
        async with self._prompt_lock:
            self._in_flight_prompt_ids.discard(prompt_id)
            if not self.is_busy:
                if self._status.state == "busy":
                    await self._emit_status("idle", reason="prompt_send_failed")
                self._settle_deferred_restart()

    async def send_message(self, message: str) -> None:
        """Send a message to the subprocess stdin."""
        await self.start()
        prompt_id: str | None = None
        try:
            in_message = JSONRPCInMessageAdapter.validate_json(message)
            async with self._prompt_lock:
                # Re-read under the same lock restart uses for its final busy
                # check. Registration therefore happens either before a
                # non-forced restart (which defers) or on its replacement.
                process = self._process
                if process is None or process.stdin is None:
                    raise RuntimeError(
                        f"session {self.session_id} has no running worker to send to"
                    )

                if isinstance(in_message, JSONRPCPromptMessage):
                    prompt_id = in_message.id
                    was_busy = self.is_busy
                    self._in_flight_prompt_ids.add(prompt_id)
                    if not was_busy:
                        await self._emit_status("busy", reason="prompt")
                elif isinstance(in_message, JSONRPCSuccessResponse | JSONRPCErrorResponse):
                    self._note_answered(in_message.id)
                elif isinstance(in_message, JSONRPCCancelMessage) and not self.is_busy:
                    # If not busy, return success to avoid errors
                    await self._broadcast(
                        JSONRPCSuccessResponse(id=in_message.id, result={}).model_dump_json()
                    )
                    return

            new_message = await self._handle_in_message(in_message)
            if new_message is not None:
                message = new_message

            if isinstance(in_message, JSONRPCInitializeMessage):
                self._last_initialize = message

            process.stdin.write((message + "\n").encode("utf-8"))
            await process.stdin.drain()
        except ValueError as e:
            self._rollback_pending_sent_files()
            if prompt_id is not None:
                await self._rollback_failed_prompt(prompt_id)
            logger.error(f"{e.__class__.__name__} {e}: Invalid JSONRPC in message: {message}")
            return
        except BaseException:
            # Encoding and stdin failures both mean this prompt was not
            # delivered. Remove only its ID: another prompt may still be live.
            self._rollback_pending_sent_files()
            if prompt_id is not None:
                await self._rollback_failed_prompt(prompt_id)
            raise
        else:
            self._pending_sent = None

        # A forwarded cancel arms the watchdog: whatever the worker does with
        # it, the session must go non-busy within a bounded time.
        if isinstance(in_message, JSONRPCCancelMessage):
            self._arm_cancel_watchdog(set(self._in_flight_prompt_ids))


_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".css",
        ".js",
        ".ts",
        ".py",
        ".sh",
        ".csv",
        ".log",
        ".rst",
        ".toml",
        ".ini",
    }
)

#: Where the composer placed an uploaded file in the message text.
_ATTACHMENT_MARKER = re.compile(r'<attachment file="([^"<>/\\]+)"/>')

#: How long every page may be closed before a retrying turn is stopped.
UNATTENDED_RETRY_GRACE_S = 180.0


class KimoCLIRunner:
    """Manages multiple session processes."""

    def __init__(self) -> None:
        """Initialize the runner."""
        self._sessions: dict[UUID, SessionProcess] = {}
        self._lock = asyncio.Lock()
        self.activity = ActivityHub()
        self._background: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        """Start the runner (no-op, sessions started on demand)."""
        pass

    async def stop(self) -> None:
        """Stop all running sessions."""
        tasks: list[asyncio.Task[None]] = []
        for session in self._sessions.values():
            if session.is_running:
                tasks.append(asyncio.create_task(session.stop()))
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5.0)
            for t in pending:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    async def get_or_create_session(self, session_id: UUID) -> SessionProcess:
        """Get or create a session process."""
        async with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = SessionProcess(
                    session_id,
                    on_activity=self.activity.publish,
                    on_retry=self._stop_retry_if_unattended,
                )
            return self._sessions[session_id]

    def _stop_retry_if_unattended(self, process: SessionProcess) -> None:
        """Called on every retry: give up once no page has been open for a while.

        A web session retries a failing model call with no attempt limit,
        because the person can stop it. Nobody can once every page is closed,
        so after ``UNATTENDED_RETRY_GRACE_S`` the retry is stopped the way the
        stop button would. A retry fires at least once a minute (the backoff
        is capped), so checking here needs no timer of its own. A turn that
        is making progress is never touched — only one that is retrying.
        """
        if self.activity.unattended_for() < UNATTENDED_RETRY_GRACE_S:
            return
        task = asyncio.get_running_loop().create_task(
            process.cancel_turn("no page open while retrying")
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def activity_snapshot(self) -> list[SessionActivity]:
        """The current activity of every session this server has a process for."""
        return [process.activity for process in self._sessions.values()]

    def get_session(self, session_id: UUID) -> SessionProcess | None:
        """Get a session process if it exists."""
        return self._sessions.get(session_id)

    async def forget_session(self, session_id: UUID) -> None:
        """Drop a deleted session's process from the table.

        Nothing ever removed an entry: deleting a session stopped its worker
        and removed its directory, and left the SessionProcess here forever.
        get_session() then kept handing back a process for a session that no
        longer exists, and every restart_running_workers /
        apply_compaction_ratio walked the accumulated dead entries — on a
        long-lived multi-user server, without bound.
        """
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            # Deletion normally stops first, but forget is also a lifecycle
            # boundary: invalidate any delayed recovery even in direct use.
            await session.stop_worker(reason="forget", emit_status=False)

    async def detach_websocket(self, ws: WebSocket, session_id: UUID) -> None:
        """Detach a WebSocket from a session."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session:
                await session.remove_websocket(ws)

    async def apply_compaction_ratio(self, ratio: float) -> list[UUID]:
        """Retune every live worker in place; no restart, no interruption."""
        async with self._lock:
            running = [(sid, proc) for sid, proc in self._sessions.items() if proc.is_running]
        applied: list[UUID] = []
        for session_id, proc in running:
            if await proc.apply_compaction_ratio(ratio):
                applied.append(session_id)
        return applied

    async def restart_running_workers(
        self,
        *,
        reason: str,
        force: bool,
        skip_model_override: bool = False,
    ) -> RestartWorkersSummary:
        """Restart all running workers to apply global config updates.

        Args:
            reason: Reason for the restart (e.g., "config_update")
            force: If True, also restart busy sessions (may interrupt prompts)
            skip_model_override: If True, sessions that have a per-session
                model override are not restarted (their model does not depend
                on the global default model).

        Returns:
            Summary of restarted and skipped sessions
        """
        async with self._lock:
            running = [(sid, proc) for sid, proc in self._sessions.items() if proc.is_running]

        restarted: list[UUID] = []
        skipped_busy: list[UUID] = []
        tasks: list[asyncio.Task[None]] = []

        for session_id, proc in running:
            if skip_model_override:
                joint = load_session_by_id(session_id)
                if joint is not None and joint.model:
                    continue
            if proc.is_busy and not force:
                # Not "not applied": applied as soon as the prompt in flight
                # finishes, without interrupting it.
                proc.defer_restart(reason)
                skipped_busy.append(session_id)
                continue
            restarted.append(session_id)
            if force:
                tasks.append(asyncio.create_task(proc.restart_worker(reason=reason, force=True)))
            else:
                tasks.append(asyncio.create_task(proc.restart_worker(reason=reason)))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        return RestartWorkersSummary(
            restarted_session_ids=restarted,
            skipped_busy_session_ids=skipped_busy,
        )


@dataclass(slots=True)
class RestartWorkersSummary:
    """Summary of a restart_running_workers operation."""

    restarted_session_ids: list[UUID]
    skipped_busy_session_ids: list[UUID]
