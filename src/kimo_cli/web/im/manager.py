"""Bridge manager: runs one adapter task per enabled channel, tracks status.

Lifecycle follows the web app's lifespan (see web/app.py): ``start()`` at
startup, ``stop()`` at shutdown. Config mutations from the API call
``apply_channel`` / ``remove_channel`` to restart only the affected adapter.
"""

from __future__ import annotations

import asyncio
import contextlib

from kimo_cli import share
from kimo_cli.utils.logging import logger
from kimo_cli.web.im import db
from kimo_cli.web.im.adapter import AdapterContext, ImAdapter
from kimo_cli.web.im.dingtalk_adapter import DingTalkAdapter
from kimo_cli.web.im.feishu_adapter import FeishuAdapter
from kimo_cli.web.im.runner import ImTurnRunner

_CHANNEL_LABELS = {"feishu": "飞书", "dingtalk": "钉钉"}
SUPPORTED_CHANNEL_TYPES = tuple(_CHANNEL_LABELS)


class _ChannelRuntime:
    def __init__(self, task: asyncio.Task, status: dict) -> None:
        self.task = task
        self.status = status
        """Mutable dict {"running": bool, "last_error": str | None} for the UI."""


class ImBridgeManager:
    def __init__(self) -> None:
        self._runner = ImTurnRunner()
        self._runtimes: dict[int, _ChannelRuntime] = {}
        # Final status of channels whose task has ended: a crashed adapter's
        # error must stay visible in the UI instead of resetting to blank.
        self._last: dict[int, dict] = {}

    # ------------------------------------------------------------------ #
    def _build_adapter(self, channel: db.ImChannelRow) -> ImAdapter | None:
        media_dir = share.get_share_dir() / "im_bridge" / f"ch{channel.id}" / "media"
        if channel.type == "feishu":
            return FeishuAdapter(channel.app_id, channel.app_secret, media_dir)
        if channel.type == "dingtalk":
            return DingTalkAdapter(channel.app_id, channel.app_secret, media_dir)
        logger.warning("Unknown IM channel type: {type}", type=channel.type)
        return None

    def _make_context(self, channel: db.ImChannelRow, status: dict) -> AdapterContext:
        def on_status(last_error: str | None) -> None:
            status["running"] = last_error is None
            status["last_error"] = last_error

        async def is_whitelisted(external_id: str) -> bool:
            return db.is_whitelisted(channel.id, external_id)

        async def run_turn(external_id: str, display_name: str, user_input: list) -> str:
            return await self._runner.run_turn(
                channel_id=channel.id,
                channel_label=_CHANNEL_LABELS.get(channel.type, channel.type),
                external_id=external_id,
                display_name=display_name,
                user_input=user_input,
                timeout_seconds=float(channel.config.get("timeout_seconds", 600)),
            )

        async def drop_session(external_id: str) -> None:
            await self._runner.drop_session(channel.id, external_id)

        async def record_pending(external_id: str, display_name: str) -> None:
            db.record_pending(channel.id, external_id, display_name)

        ctx = AdapterContext(
            channel_id=channel.id,
            channel_name=channel.name,
            config=channel.config,
            is_whitelisted=is_whitelisted,
            run_turn=run_turn,
            drop_session=drop_session,
            record_pending=record_pending,
            on_status=on_status,
        )
        return ctx

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        for channel in db.list_channels():
            if channel.enabled:
                self._spawn(channel)

    async def stop(self) -> None:
        for runtime in self._runtimes.values():
            runtime.task.cancel()
        for runtime in self._runtimes.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runtime.task
        self._runtimes.clear()

    def _spawn(self, channel: db.ImChannelRow) -> None:
        adapter = self._build_adapter(channel)
        if adapter is None:
            return
        status = {"running": False, "last_error": None}

        async def run() -> None:
            ctx = self._make_context(channel, status)
            try:
                await adapter.start(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                ctx.on_status(str(e))
                logger.exception(
                    "IM channel {id} ({name}) crashed", id=channel.id, name=channel.name
                )

        task = asyncio.create_task(run())
        runtime = _ChannelRuntime(task, status)
        task.add_done_callback(lambda t: self._remember(channel.id, runtime))
        self._runtimes[channel.id] = runtime

    def _remember(self, channel_id: int, runtime: _ChannelRuntime) -> None:
        self._runtimes.pop(channel_id, None)
        status = dict(runtime.status)
        status["running"] = False  # a finished task is never running
        self._last[channel_id] = status

    async def restart_channel(self, channel_id: int) -> None:
        """Restart a channel's adapter (after create/update/enable)."""
        runtime = self._runtimes.pop(channel_id, None)
        if runtime is not None:
            runtime.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runtime.task
        channel = db.get_channel(channel_id)
        if channel is not None and channel.enabled:
            self._spawn(channel)

    async def remove_channel(self, channel_id: int) -> None:
        runtime = self._runtimes.pop(channel_id, None)
        self._last.pop(channel_id, None)
        if runtime is not None:
            runtime.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runtime.task

    def status(self, channel_id: int) -> dict:
        runtime = self._runtimes.get(channel_id)
        if runtime is not None:
            return dict(runtime.status)
        return dict(self._last.get(channel_id, {"running": False, "last_error": None}))
