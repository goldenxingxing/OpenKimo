"""Adapter protocol between IM platforms and the OpenKimo-side bridge runtime.

An adapter owns the platform's long connection and translates platform events
into calls on the :class:`AdapterContext`; the manager supplies one context per
running channel.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


async def _noop() -> None:
    return None


@dataclass
class AdapterContext:
    """Callbacks an adapter uses to talk back to the bridge runtime.

    ``channel_id``/``channel_name`` identify the channel; ``config`` is the
    channel's merged config dict (show_thinking etc.).
    """

    channel_id: int
    channel_name: str
    config: dict[str, Any]
    is_whitelisted: Callable[[str], Awaitable[bool]]
    run_turn: Callable[[str, str, list[dict[str, Any]]], Awaitable[str]]
    """run_turn(external_id, display_name, user_input_parts) -> reply text."""
    drop_session: Callable[[str], Awaitable[None]]
    """drop_session(external_id) resets the binding (the IM /reset command)."""
    record_pending: Callable[[str, str], Awaitable[None]] = lambda _e, _n: _noop()
    """record_pending(external_id, display_name) logs a stranger for the UI's
    one-click authorization list."""
    on_status: Callable[[str | None], None] = field(default=lambda _e: None)
    """on_status(last_error) reports the channel's runtime status for the UI."""


class ImAdapter(Protocol):
    """A platform adapter (Feishu first). ``start`` runs until stopped."""

    async def start(self, ctx: AdapterContext) -> None: ...

    async def stop(self) -> None: ...
