"""Fan-out of every session's run state, for a page that is not watching it.

A page streams one session over its WebSocket and learns about the others only
by polling the session list — which it stops doing while the tab is hidden. So
a session that finished, or stopped to ask for an approval, went unnoticed until
someone looked. This hub carries each session's state to every open page.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from kimo_cli.web.models import SessionState

#: Per-subscriber backlog. A page that stops reading falls behind by this much
#: before its oldest entries are dropped; only the latest state per session
#: matters to it, so dropping is safe where blocking the publisher is not.
_SUBSCRIBER_BACKLOG = 256


class SessionActivity(BaseModel):
    """What a page needs to know to tell whether a session is waiting on a person."""

    session_id: UUID
    state: SessionState
    #: True while the session holds an approval or a question nobody has answered.
    awaiting_input: bool = False
    seq: int = Field(..., description="Monotonic per session")
    updated_at: datetime


class ActivityHub:
    """Publish session activity to any number of subscribers without blocking."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[SessionActivity]] = set()
        # Every open page subscribes, so this doubles as "is anyone here".
        # Starts unattended: a server nobody has opened a page on yet has
        # nobody to stop a retry for it.
        self._unattended_since: float | None = time.monotonic()

    def subscribe(self) -> asyncio.Queue[SessionActivity]:
        queue: asyncio.Queue[SessionActivity] = asyncio.Queue(maxsize=_SUBSCRIBER_BACKLOG)
        self._subscribers.add(queue)
        self._unattended_since = None
        return queue

    def unsubscribe(self, queue: asyncio.Queue[SessionActivity]) -> None:
        self._subscribers.discard(queue)
        if not self._subscribers and self._unattended_since is None:
            self._unattended_since = time.monotonic()

    def unattended_for(self) -> float:
        """Seconds since the last page closed; 0 while any page is open."""
        if self._unattended_since is None:
            return 0.0
        return time.monotonic() - self._unattended_since

    def publish(self, activity: SessionActivity) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(activity)


__all__ = ["ActivityHub", "SessionActivity"]
