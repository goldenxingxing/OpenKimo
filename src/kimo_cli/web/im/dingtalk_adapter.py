"""DingTalk adapter: dingtalk-stream long connection, bridged in-process.

Threading model (differs from the Feishu adapter): the SDK coroutine runs
directly on the web server's loop, so no cross-thread marshalling is needed
for the message pipeline — ``run_coroutine_threadsafe`` is still used at the
entry point because it is safe from any caller thread.

Teardown ruling (same as Feishu): a disabled channel must drop the long
connection, or DingTalk keeps dispatching messages to this process and shares
the bot with other agents. The SDK's own ``start()`` loop swallows every
exception — even ``CancelledError`` is caught and retried — so neither task
cancellation nor closing the websocket would stop it. :class:`_CancellableStreamClient`
mirrors that loop with a stop check; the structure is pinned to the
``dingtalk-stream`` 0.24.x line (``websockets.connect`` / ``async for`` API).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import dingtalk_stream as ds
from dingtalk_stream.frames import Headers

from kimo_cli.utils.logging import logger
from kimo_cli.web.im.adapter import AdapterContext
from kimo_cli.web.im.parser import _MAX_B64_INLINE, _sniff_image_mime

RESET_CMDS = ("/ok_reset", "/reset")
_DEDUP_TTL = 600.0
_UNAUTHORIZED_REPLY = "抱歉，你不在该机器人的授权用户名单中。"
# Robot text messages over a session webhook have a platform size limit;
# conservative per-chunk size so long agent replies arrive intact.
_REPLY_CHUNK = 1500


class _CancellableStreamClient(ds.DingTalkStreamClient):
    """DingTalkStreamClient whose receive loop honours a stop flag.

    Also surfaces connection status: the SDK is silent after the websocket
    opens, so the bridge would show "stopped" forever even while healthy.
    """

    def __init__(
        self,
        credential: ds.Credential,
        stop: threading.Event,
        on_connected=None,
        on_error=None,
    ) -> None:
        super().__init__(credential)
        self._stop_flag = stop
        self._on_connected = on_connected
        self._on_error = on_error
        # The SDK infers ``None`` as this attribute's declared type; annotate
        # it so teardown can reset it without a type error.
        self.websocket: Any = None

    async def start(self) -> None:  # noqa: D102 - mirror of SDK start(), see module docstring
        from urllib.parse import quote_plus

        import websockets

        self.pre_start()
        while not self._stop_flag.is_set():
            try:
                connection = self.open_connection()
                if not connection:
                    if self._on_error is not None:
                        self._on_error("open connection failed (check credentials)")
                    await asyncio.sleep(10)
                    continue
                uri = f"{connection['endpoint']}?ticket={quote_plus(connection['ticket'])}"
                async with websockets.connect(uri) as websocket:
                    self.websocket = websocket
                    if self._on_connected is not None:
                        self._on_connected()
                    asyncio.create_task(self.keepalive(websocket))
                    async for raw_message in websocket:
                        if self._stop_flag.is_set():
                            break
                        asyncio.create_task(self.background_task(json.loads(raw_message)))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._on_error is not None:
                    self._on_error(str(e))
                await asyncio.sleep(3)


class _BotHandler(ds.ChatbotHandler):
    """ACK incoming callbacks immediately; the turn runs on the bridge loop."""

    def __init__(self, adapter: DingTalkAdapter) -> None:
        super().__init__()
        self._adapter = adapter

    async def raw_process(self, callback_message: ds.CallbackMessage) -> ds.AckMessage:
        ack = ds.AckMessage()
        ack.code = ds.AckMessage.STATUS_OK
        # The SDK's Headers infers None-typed fields; go through Any for the
        # same reason as websocket above.
        headers: Any = ack.headers
        headers.message_id = callback_message.headers.message_id
        headers.content_type = Headers.CONTENT_TYPE_APPLICATION_JSON
        try:
            incoming = ds.ChatbotMessage.from_dict(callback_message.data)
        except Exception:
            logger.exception("dingtalk-bridge message parse failed")
            return ack
        ack.message = "ok"
        loop = self._adapter._loop
        if loop is not None:
            asyncio.run_coroutine_threadsafe(self._adapter.handle(incoming), loop)
        return ack


class DingTalkAdapter:
    def __init__(self, client_id: str, client_secret: str, media_dir: Path) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._media_dir = media_dir
        self._ctx: AdapterContext | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seen: dict[str, float] = {}
        self._busy: set[str] = set()
        self._stop = threading.Event()
        self._client: _CancellableStreamClient | None = None
        self._handler: _BotHandler | None = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self, ctx: AdapterContext) -> None:
        self._ctx = ctx
        self._loop = asyncio.get_running_loop()
        self._stop.clear()

        def _connected() -> None:
            ctx.on_status(None)
            logger.info("dingtalk-bridge connected: channel={id}", id=ctx.channel_id)

        def _error(message: str) -> None:
            ctx.on_status(message)

        client = _CancellableStreamClient(
            ds.Credential(self._client_id, self._client_secret),
            self._stop,
            on_connected=_connected,
            on_error=_error,
        )
        self._client = client
        self._handler = _BotHandler(self)
        client.register_callback_handler(ds.ChatbotMessage.TOPIC, self._handler)
        logger.info(
            "dingtalk-bridge starting: channel={name} ({id})",
            name=ctx.channel_name,
            id=ctx.channel_id,
        )
        try:
            await client.start()
        finally:
            # Disabled or cancelled: drop late events and close the socket so
            # the platform stops dispatching here (same ruling as Feishu).
            self._ctx = None
            client.callback_handler_map.clear()
            ws = client.websocket
            client.websocket = None
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
            logger.info("dingtalk-bridge stopped: channel={id}", id=ctx.channel_id)

    async def stop(self) -> None:
        """Ask the receive loop to exit (the manager also cancels the task)."""
        self._stop.set()
        client = self._client
        if client is not None and client.websocket is not None:
            with contextlib.suppress(Exception):
                await client.websocket.close()

    # ------------------------------------------------------------------ #
    # message pipeline
    # ------------------------------------------------------------------ #
    async def handle(self, incoming: ds.ChatbotMessage) -> None:
        ctx = self._ctx
        if ctx is None:
            return
        message_id = incoming.message_id or ""
        if not message_id or self._is_duplicate(message_id):
            return
        external_id = incoming.sender_staff_id or incoming.sender_id or ""
        if not external_id:
            return
        # Group chats only answer @mentions, and only when enabled in config.
        if incoming.conversation_type == "2":
            if not ctx.config.get("group_chat_enabled", False):
                return
            if not incoming.is_in_at_list:
                return
        if external_id in self._busy:
            await self._reply(incoming, "上一条消息还在处理中，请稍候～")
            return
        self._busy.add(external_id)
        try:
            await self._handle_inner(ctx, incoming, external_id)
        finally:
            self._busy.discard(external_id)

    async def _handle_inner(
        self, ctx: AdapterContext, incoming: ds.ChatbotMessage, external_id: str
    ) -> None:
        # Whitelist is the trust boundary: outsiders get a refusal and are
        # recorded so the web UI can authorize them with one click.
        if not await ctx.is_whitelisted(external_id):
            await ctx.record_pending(external_id, incoming.sender_nick or external_id)
            await self._reply(incoming, _UNAUTHORIZED_REPLY)
            return

        user_input = await self._parse(incoming)
        if not user_input:
            return

        plain = "\n".join(p["text"] for p in user_input if p.get("type") == "text").strip()
        if plain in RESET_CMDS:
            await ctx.drop_session(external_id)
            await self._reply(incoming, "已重置本会话对应的 OpenKimo 上下文。")
            return

        if ctx.config.get("show_thinking", True):
            await self._reply(incoming, "🤔 正在思考中…")

        try:
            reply = await ctx.run_turn(external_id, incoming.sender_nick or external_id, user_input)
        except Exception as e:
            logger.exception("dingtalk-bridge turn failed")
            await self._reply(incoming, f"❌ 处理失败: {e}")
            return

        await self._reply(incoming, reply or "（OpenKimo 未返回文本内容）")

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _is_duplicate(self, message_id: str) -> bool:
        now = time.time()
        for mid, ts in list(self._seen.items()):
            if now - ts > _DEDUP_TTL:
                del self._seen[mid]
        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        return False

    async def _parse(self, incoming: ds.ChatbotMessage) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        text_bits: list[str] = []
        msg_type = incoming.message_type or ""
        if msg_type == "text":
            text_bits.append(incoming.text.content if incoming.text else "")
        elif msg_type == "richText":
            text_bits.extend(incoming.get_text_list() or [])
        elif msg_type == "picture":
            # Download is blocking (requests): keep it off the event loop.
            await asyncio.to_thread(self._handle_image_sync, incoming, parts, text_bits)
        else:
            text_bits.append(f"[不支持的消息类型: {msg_type}]")
        if text_bits:
            text = "\n".join(t for t in text_bits if t)
            if text:
                parts.insert(0, {"type": "text", "text": text})
        return parts

    def _handle_image_sync(
        self, incoming: ds.ChatbotMessage, parts: list[dict], text_bits: list[str]
    ) -> None:
        """Picture message: download inline as data URL, or save to media dir.

        Runs in a worker thread (see ``_parse``); on failure leaves a text
        hint, mirroring the Feishu parser contract.
        """
        handler = self._handler
        if handler is None:
            return
        try:
            codes = incoming.get_image_list() or []
            if not codes:
                text_bits.append("[图片下载失败]")
                return
            url = handler.get_image_download_url(codes[0])
            if not url:
                text_bits.append("[图片下载失败]")
                return
            import requests

            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.content
            b64 = base64.b64encode(data).decode("ascii")
            if len(b64) <= _MAX_B64_INLINE:
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{_sniff_image_mime(b64)};base64,{b64}"},
                    }
                )
            else:
                path = self._media_dir / f"{uuid.uuid4().hex[:8]}_image.jpg"
                path.write_bytes(data)
                text_bits.append(
                    f"[用户发送了一张图片，已保存到: {path}，请用 ReadMediaFile 工具查看内容]"
                )
        except Exception:
            logger.exception("dingtalk-bridge image download failed")
            text_bits.append("[图片下载失败]")

    async def _reply(self, incoming: ds.ChatbotMessage, text: str) -> None:
        handler = self._handler
        if handler is None:
            return
        for chunk in _chunks(text, _REPLY_CHUNK):
            try:
                await asyncio.to_thread(handler.reply_text, chunk, incoming)
            except Exception:
                logger.exception("dingtalk-bridge reply failed")


def _chunks(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    return [text[i : i + size] for i in range(0, len(text), size)]
