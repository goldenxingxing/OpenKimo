"""In-process turn execution for IM bridge sessions.

Each IM sender (channel + external_id) maps to one ordinary OpenKimo session in
its own workspace directory, so the web session view carries the conversation
automatically. Sessions are created with the approval gate lifted (yolo) —
that is the user's ruling: the IM whitelist IS the trust boundary.

The runner goes through ``KimoCLI`` (ui_mode="wire") directly instead of the
web runner's worker subprocesses: no HTTP/WS loopback, no child process. The
trade-off, accepted for v1: no live streaming of IM turns into an open web
view (history appears once the turn lands on disk).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import cast

from kaos.path import KaosPath
from kosong.message import ContentPart, TextPart

from kimo_cli import share
from kimo_cli.app import KimoCLI
from kimo_cli.session import Session
from kimo_cli.session import Session as KimoCLISession
from kimo_cli.session_state import load_session_state, save_session_state
from kimo_cli.utils.aioqueue import QueueShutDown
from kimo_cli.utils.logging import logger
from kimo_cli.web.im import db
from kimo_cli.wire.types import QuestionRequest


def workspace_path(channel_id: int, external_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", external_id)[:48] or "user"
    return share.get_share_dir() / "im_bridge" / "workspaces" / f"ch{channel_id}_{safe}"


async def _load_or_create_session(
    channel_id: int, external_id: str, display_name: str, channel_label: str
) -> Session:
    """Return the session bound to this sender, creating and binding one if needed."""
    work_dir_path = workspace_path(channel_id, external_id)
    work_dir = KaosPath.unsafe_from_local_path(work_dir_path)
    bound_id = db.get_binding(channel_id, external_id)

    session: Session | None = None
    if bound_id is not None:
        # Load the bound session directly. Session.continue_() is unusable
        # here: it resolves the session via the global metadata's
        # ``last_session_id``, which is only written by the web runner's
        # create-session path — IM-created sessions never set it, so every
        # message would look "missing" and spawn a fresh session (context
        # loss on every turn after the first).
        candidate = await KimoCLISession.find(work_dir, bound_id)
        if candidate is not None:
            session = candidate
        else:
            logger.warning(
                "IM binding {channel}/{external} points at a missing session"
                " ({bound}); creating a fresh one",
                channel=channel_id,
                external=external_id,
                bound=bound_id,
            )

    if session is None:
        work_dir_path.mkdir(parents=True, exist_ok=True)
        session = await KimoCLISession.create(work_dir=work_dir)
        state = load_session_state(session.dir)
        state.approval.yolo = True
        label = f"{channel_label} {display_name}".strip() or external_id
        state.custom_title = f"[IM] {label}"[:120]
        save_session_state(state, session.dir)
        db.set_binding(channel_id, external_id, session.id, display_name)
        logger.info(
            "IM session created: channel={channel} external={external} session={sid}",
            channel=channel_id,
            external=external_id,
            sid=session.id,
        )
    return session


class ImTurnRunner:
    """Serializes and executes turns per sender binding."""

    def __init__(self) -> None:
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}

    def _lock_for(self, channel_id: int, external_id: str) -> asyncio.Lock:
        key = (channel_id, external_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def run_turn(
        self,
        *,
        channel_id: int,
        channel_label: str,
        external_id: str,
        display_name: str,
        user_input: list[dict],
        timeout_seconds: float,
    ) -> str:
        """Run one turn; returns the assistant's reply text (may be empty)."""
        async with self._lock_for(channel_id, external_id):
            session = await _load_or_create_session(
                channel_id, external_id, display_name, channel_label
            )
            return await asyncio.wait_for(self._run(session, user_input), timeout=timeout_seconds)

    async def _run(self, session: Session, user_input: list[dict]) -> str:
        texts: list[str] = []
        cancel_event = asyncio.Event()
        kimo = await KimoCLI.create(session, yolo=True, ui_mode="wire")
        question_task = asyncio.create_task(self._answer_questions(kimo))
        try:
            async for msg in kimo.run(cast("list[ContentPart]", user_input), cancel_event):
                if isinstance(msg, TextPart):
                    texts.append(msg.text)
        finally:
            question_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await question_task
            await kimo.close()
        return "".join(texts).strip()

    @staticmethod
    async def _answer_questions(kimo: KimoCLI) -> None:
        """Auto-answer interactive questions so IM turns never hang on them.

        IM has no channel to carry a question to the user or an answer back:
        without this, AskUserQuestion sits unanswered until the turn hits the
        channel timeout. Resolving with a note tells the model to ask the
        question in its plain-text reply instead, where the user answers in
        their next message — which keeps the conversation flowing.
        """
        runtime = getattr(kimo, "_runtime", None)
        hub = getattr(runtime, "root_wire_hub", None)
        if hub is None:
            return
        note = (
            "（IM 渠道不支持交互式选择；请把这个问题直接写进给用户的回复正文，"
            "用户会在下一条消息里用文字回答。）"
        )
        queue = hub.subscribe()
        try:
            while True:
                msg = await queue.get()
                if isinstance(msg, QuestionRequest):
                    msg.resolve({q.question: note for q in msg.questions})
        except QueueShutDown:
            pass
        finally:
            hub.unsubscribe(queue)

    async def drop_session(self, channel_id: int, external_id: str) -> None:
        async with self._lock_for(channel_id, external_id):
            db.drop_binding(channel_id, external_id)
