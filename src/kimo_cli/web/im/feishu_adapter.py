"""Feishu adapter: lark-oapi websocket long connection, bridged in-process.

Ported from integrations/feishu-bridge/feishu_bridge/bot.py (QLi, 2026-11-18,
production-tested). Threading model kept:

- the lark SDK runs its ws receive loop on its own thread; event callbacks fire
  synchronously there and submit coroutines to the bridge's main event loop.
- the SDK creates a module-level event loop at import time and drives its
  receive loop with it: a second new_event_loop() would leave receiving dead.
  ``start()`` is therefore awaited from the web server's loop and reuses the
  SDK's loop via ``asyncio.get_event_loop()`` semantics — same as the
  standalone bridge's ``run()``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from kimo_cli.utils.logging import logger
from kimo_cli.web.im.adapter import AdapterContext
from kimo_cli.web.im.feishu_api import FeishuAPI
from kimo_cli.web.im.parser import MessageParser

RESET_CMDS = ("/ok_reset", "/reset")
_DEDUP_TTL = 600.0
_UNAUTHORIZED_REPLY = "抱歉，你不在该机器人的授权用户名单中。"


class FeishuAdapter:
    def __init__(self, app_id: str, app_secret: str, media_dir: Path) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._media_dir = media_dir
        self._ctx: AdapterContext | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seen: dict[str, float] = {}
        self._busy: set[str] = set()
        self._stop = asyncio.Event()
        self._client = None

    # ------------------------------------------------------------------ #
    # lark event callback (SDK thread, synchronous)
    # ------------------------------------------------------------------ #
    def on_message(self, data) -> None:
        ctx = self._ctx
        if ctx is None or self._loop is None:
            return
        try:
            event = data.event
            if event is None or event.message is None:
                return
            message = event.message
            message_id = message.message_id or ""
            if not message_id or self._is_duplicate(message_id):
                return
            sender = event.sender
            if sender is None or sender.sender_id is None:
                return
            open_id = sender.sender_id.open_id or ""
            if not open_id:
                return
            chat_type = message.chat_type or "p2p"
            if chat_type != "p2p" and not ctx.config.get("group_chat_enabled", False):
                return

            coro = self._handle(ctx, open_id, message_id, message)
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            future.add_done_callback(self._on_handle_done)
        except Exception:
            logger.exception("feishu-bridge on_message error")

    @staticmethod
    def _on_handle_done(future) -> None:
        try:
            future.result()
        except Exception:
            logger.exception("feishu-bridge message handler crashed")

    def _is_duplicate(self, message_id: str) -> bool:
        now = time.time()
        for mid, ts in list(self._seen.items()):
            if now - ts > _DEDUP_TTL:
                del self._seen[mid]
        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        return False

    # ------------------------------------------------------------------ #
    # message handling coroutines (main loop)
    # ------------------------------------------------------------------ #
    async def _handle(self, ctx: AdapterContext, open_id: str, message_id: str, message) -> None:
        if open_id in self._busy:
            try:
                await self._api.reply_text(message_id, "上一条消息还在处理中，请稍候～")
            except Exception:
                logger.exception("feishu-bridge busy reply failed")
            return
        self._busy.add(open_id)
        try:
            await self._handle_inner(ctx, open_id, message_id, message)
        finally:
            self._busy.discard(open_id)

    async def _handle_inner(
        self, ctx: AdapterContext, open_id: str, message_id: str, message
    ) -> None:
        api = self._api
        # Whitelist is the trust boundary (user ruling): outsiders get a
        # refusal, and are recorded so the web UI can authorize them with one
        # click instead of hunting for their open_id.
        if not await ctx.is_whitelisted(open_id):
            await ctx.record_pending(open_id, open_id)
            await api.reply_text(message_id, _UNAUTHORIZED_REPLY)
            return

        try:
            user_input = await self._parser.parse(
                message_id, message.message_type or "", message.content or ""
            )
        except Exception:
            logger.exception("feishu-bridge message parse failed")
            await api.reply_text(message_id, "❌ 消息解析失败，请稍后重试")
            return

        if not user_input:
            return

        plain = self._plain_text(user_input)
        if plain in RESET_CMDS:
            await ctx.drop_session(open_id)
            await api.reply_text(message_id, "已重置本会话对应的 OpenKimo 上下文。")
            return

        if ctx.config.get("show_thinking", True):
            try:
                await api.reply_text(message_id, "🤔 正在思考中…")
            except Exception:
                logger.exception("feishu-bridge thinking hint failed")

        try:
            reply = await ctx.run_turn(open_id, open_id, user_input)
        except Exception as e:
            logger.exception("feishu-bridge turn failed")
            await api.reply_text(message_id, f"❌ 处理失败: {e}")
            return

        if not reply:
            reply = "（OpenKimo 未返回文本内容）"
        await api.reply_text(message_id, reply)

    @staticmethod
    def _plain_text(user_input: list) -> str:
        return "\n".join(
            p["text"] for p in user_input if isinstance(p, dict) and p.get("type") == "text"
        ).strip()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self, ctx: AdapterContext) -> None:
        import lark_oapi as lark

        self._ctx = ctx
        self._api = FeishuAPI(self._app_id, self._app_secret)
        self._parser = MessageParser(self._api, self._media_dir)
        self._stop = asyncio.Event()

        # The lark SDK creates a module-level event loop at import time and,
        # per the upstream bridge's hard-won notes, must not be given a second
        # fresh loop. We go one step further than the standalone bridge: this
        # start() runs *inside* the web server's loop, so ``client._connect()``
        # and its receive tasks are driven by uvicorn directly, and event
        # callbacks (fired on the SDK's ws thread) are marshalled back onto
        # this captured loop — never asyncio.get_event_loop() in that thread.
        self._loop = asyncio.get_running_loop()

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.on_message)
            .build()
        )
        client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        logger.info(
            "feishu-bridge starting: channel={name} ({id})",
            name=ctx.channel_name,
            id=ctx.channel_id,
        )
        # Private SDK API, pinned by ``lark-oapi>=1.4.0,<2`` (same as the
        # standalone bridge; start() has known loop-affinity issues).
        self._client = client
        try:
            await client._connect()
            ctx.on_status(None)
            logger.info("feishu-bridge connected: channel={id}", id=ctx.channel_id)
            await self._stop.wait()
        finally:
            # Task cancellation alone does NOT close the websocket: the SDK's
            # receive loop holds the connection and would keep receiving (and
            # auto-reconnecting) after the channel was disabled, so Feishu
            # would keep dispatching messages to this process and share them
            # with any other agent bridged to the same bot. Disable reconnect
            # first — _disconnect() makes the receive loop raise, and without
            # the flag it would immediately reconnect — then close the socket.
            self._ctx = None
            try:
                client._auto_reconnect = False
                await client._disconnect()
            except Exception:
                logger.exception("feishu-bridge disconnect failed: channel={id}", id=ctx.channel_id)

    async def stop(self) -> None:
        self._stop.set()
