"""OpenKimo Web API 客户端：会话管理 + WS 对话。

协议（实测确认）：
- POST /api/sessions/           创建会话，返回 {"session_id": ...}
- WS  /api/sessions/{id}/stream 握手后先收 history_complete 才能发 prompt
- prompt: {"method":"prompt","id":...,"params":{"user_input": ...}}
  user_input 可为 str 或 ContentPart list（text/image_url/audio_url/video_url）
- 回复事件: {"method":"event","params":{"type":"ContentPart","payload":{...}}}
- turn 结束: params.type == "TurnEnd"

前置条件：OpenKimo >= 0.4.10 源码补丁——web 会话默认 approval.yolo=True，
否则 LLM 调用工具时会永久等待审批。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import aiohttp

HISTORY_COMPLETE = "history_complete"


class OpenKimoClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _ws_url(self, session_id: str) -> str:
        base = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        sep = "&" if "?" in base else "?"
        token_q = f"token={self.token}" if self.token else ""
        return f"{base}/api/sessions/{session_id}/stream{sep}{token_q}"

    async def create_session(self) -> str | None:
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as sess:
                async with sess.post(
                    f"{self.base_url}/api/sessions/", json={}, headers=self._headers()
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    sid = data.get("session_id") or data.get("id")
                    if not sid and isinstance(data.get("kimi_cli_session"), dict):
                        sid = data["kimi_cli_session"].get("id")
                    return str(sid) if sid else None
        except Exception as e:
            print(f"[openkimo] create session failed: {e}")
            return None

    async def prompt(self, session_id: str, user_input: Any) -> str:
        """发一条 prompt，收集文本回复直到 TurnEnd。user_input 为 str 或 part list。"""
        parts: list[str] = []
        prompt_id = str(uuid.uuid4())
        sent = False
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        ) as sess:
            async with sess.ws_connect(
                self._ws_url(session_id), headers=self._headers(), heartbeat=30
            ) as ws:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        parsed = self._parse_event(msg.data)
                        if not parsed:
                            continue
                        etype, payload = parsed
                        if not sent:
                            if etype == HISTORY_COMPLETE:
                                await ws.send_json({
                                    "method": "prompt",
                                    "id": prompt_id,
                                    "params": {"user_input": user_input},
                                })
                                sent = True
                            continue
                        if etype == "ContentPart":
                            pt = payload.get("type") if isinstance(payload, dict) else None
                            if pt == "text":
                                t = payload.get("text", "")
                                if isinstance(t, str):
                                    parts.append(t)
                        elif etype == "TurnEnd":
                            break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
        return "".join(parts).strip()

    @staticmethod
    def _parse_event(raw: str) -> tuple[str, dict[str, Any]] | None:
        """OpenKimo WS 事件格式：{"jsonrpc":"2.0","method":"event","params":{...}}。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict) and data.get("method") == "event":
            params = data.get("params") or {}
            etype = params.get("type")
            payload = params.get("payload")
            if etype and isinstance(payload, dict):
                return etype, payload
        if data.get("method") == HISTORY_COMPLETE:
            return HISTORY_COMPLETE, {}
        if isinstance(data.get("type"), str):
            return data["type"], data
        return None
