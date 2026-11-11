from __future__ import annotations

from kimo_cli.utils.aioqueue import Queue
from kimo_cli.utils.broadcast import BroadcastQueue
from kimo_cli.wire.types import WireMessage


class RootWireHub:
    """Session-level broadcast hub for out-of-turn wire messages."""

    def __init__(self) -> None:
        self._queue = BroadcastQueue[WireMessage]()

    def subscribe(self) -> Queue[WireMessage]:
        return self._queue.subscribe()

    def unsubscribe(self, queue: Queue[WireMessage]) -> None:
        self._queue.unsubscribe(queue)

    async def publish(self, msg: WireMessage) -> None:
        await self._queue.publish(msg)

    def publish_nowait(self, msg: WireMessage) -> None:
        self._queue.publish_nowait(msg)

    def shutdown(self) -> None:
        self._queue.shutdown()
