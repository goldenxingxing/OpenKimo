"""桥接主逻辑：lark-oapi 长连接收消息，异步编排转发 OpenKimo 并回复。

线程模型：
- lark SDK 在自己的线程跑 ws 长连接（client.start() 阻塞），事件回调同步触发；
- 回调里把协程提交到主 asyncio loop（run_coroutine_threadsafe）统一处理。
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import traceback
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from .feishu_api import FeishuAPI
from .openkimo import OpenKimoClient
from .parser import MessageParser
from .session_map import SessionMap

RESET_CMDS = ("/ok_reset", "/reset")
_DEDUP_TTL = 600.0


class FeishuOpenKimoBridge:
    def __init__(self, cfg: dict, base_dir: Path) -> None:
        self.cfg = cfg
        data_dir = (base_dir / cfg["data_dir"]).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)

        self.api = FeishuAPI(cfg["feishu_app_id"], cfg["feishu_app_secret"])
        self.parser = MessageParser(self.api, data_dir)
        self.ok = OpenKimoClient(
            cfg["openkimo_base_url"], cfg["openkimo_token"],
            timeout=float(cfg["timeout_seconds"]),
        )
        self.sessions = SessionMap(data_dir / "session_map.json")
        self._seen: dict[str, float] = {}
        self._busy: set[str] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ #
    # lark 事件回调（SDK 线程，同步）
    # ------------------------------------------------------------------ #
    def on_message(self, data: P2ImMessageReceiveV1) -> None:
        print("[bridge] on_message fired", flush=True)
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
            if chat_type != "p2p" and not self.cfg["group_chat_enabled"]:
                return

            print(f"[feishu] recv type={message.message_type} id={message_id}")
            coro = self._handle(open_id, message_id, message)
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
            # Consume the result: an unretrieved exception here would surface
            # only as "Task exception was never retrieved" and the user would
            # get no reply and no actionable log.
            future.add_done_callback(self._on_handle_done)
        except Exception as e:
            print(f"[bridge] on_message error: {e}")

    @staticmethod
    def _on_handle_done(future) -> None:
        try:
            future.result()
        except Exception:
            traceback.print_exc()
            print("[bridge] message handler crashed (see traceback above)", flush=True)

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
    # 消息处理协程（主 loop）
    # ------------------------------------------------------------------ #
    async def _handle(self, open_id: str, message_id: str, message) -> None:
        if open_id in self._busy:
            with contextlib.suppress(Exception):
                await self.api.reply_text(message_id, "上一条消息还在处理中，请稍候～")
            return
        self._busy.add(open_id)
        try:
            await self._handle_inner(open_id, message_id, message)
        finally:
            self._busy.discard(open_id)

    async def _handle_inner(self, open_id: str, message_id: str, message) -> None:
        try:
            user_input = await self.parser.parse(
                message_id, message.message_type or "", message.content or ""
            )
        except Exception as e:
            await self.api.reply_text(message_id, f"❌ 消息解析失败: {e}")
            return

        if not user_input:
            return

        # 指令：重置会话
        plain = self._plain_text(user_input)
        if plain in RESET_CMDS:
            self.sessions.drop(open_id)
            await self.api.reply_text(message_id, "已重置本会话对应的 OpenKimo 上下文。")
            return

        if self.cfg["show_thinking"]:
            with contextlib.suppress(Exception):
                await self.api.reply_text(message_id, "🤔 正在思考中…")

        session_id = self.sessions.get(open_id) or await self._create_and_map(open_id)
        if not session_id:
            await self.api.reply_text(
                message_id, "❌ 无法创建 OpenKimo 会话，请确认 OpenKimo 正在运行。"
            )
            return

        try:
            reply = await self.ok.prompt(session_id, user_input)
        except Exception as e:
            print(f"[bridge] prompt failed for {open_id}: {e}")
            await self.api.reply_text(message_id, f"❌ OpenKimo 调用失败: {e}")
            return

        if not reply:
            reply = "（OpenKimo 未返回文本内容）"
        await self.api.reply_text(message_id, reply)
        print(f"[bridge] done open_id={open_id[:12]} reply_len={len(reply)}")

    async def _create_and_map(self, open_id: str) -> str | None:
        sid = await self.ok.create_session()
        if sid:
            self.sessions.set(open_id, sid)
        return sid

    @staticmethod
    def _plain_text(user_input: list) -> str:
        return "\n".join(
            p["text"] for p in user_input
            if isinstance(p, dict) and p.get("type") == "text"
        ).strip()

    # ------------------------------------------------------------------ #
    # 启动
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        # 关键：lark SDK 在 import 时创建全局 loop 并 set_event_loop，
        # _connect() 内部的收消息循环 create_task 用的就是那个 loop。
        # 这里必须复用它（新建 loop 会导致收消息循环永远不运行）。
        self.loop = asyncio.get_event_loop()

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.on_message)
            .build()
        )
        client = lark.ws.Client(
            self.cfg["feishu_app_id"],
            self.cfg["feishu_app_secret"],
            event_handler=handler,
            log_level=lark.LogLevel.DEBUG,
        )
        print("[bridge] starting lark ws client ...", flush=True)
        # 与 AstrBot 相同：在当前 loop 里 _connect（start() 依赖模块级全局 loop，
        # 与 set_event_loop 组合时会出现连接不建立的问题）。
        self.loop.run_until_complete(self._connect_and_forever(client))

    async def _connect_and_forever(self, client) -> None:
        await client._connect()
        print("[bridge] lark ws connected.", flush=True)
        await asyncio.Future()  # 永久挂起，事件回调提交协程到本 loop
