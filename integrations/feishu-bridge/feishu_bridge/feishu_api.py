"""飞书 OpenAPI 封装：tenant token 管理 + 消息资源下载 + 回复发送。

知识点（实测踩坑记录）：
- 消息资源下载：GET /im/v1/messages/{id}/resources/{file_key}?type=image|file
  图片类用 type=image；视频/音频/附件（含 post 顶层 files 里的）用 type=file。
- 应用需开通 im:message、im:resource 权限，事件订阅 im.message.receive_v1。
"""
from __future__ import annotations

import asyncio
import time

import aiohttp

API_BASE = "https://open.feishu.cn/open-apis"


class FeishuAPI:
    def __init__(self, app_id: str, app_secret: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: str = ""
        self._token_expire: float = 0.0
        self._lock = asyncio.Lock()

    async def tenant_token(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._token_expire - 300:
                return self._token
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as sess, sess.post(
                f"{API_BASE}/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self.app_id,
                    "app_secret": self.app_secret,
                },
            ) as resp:
                data = await resp.json()
                if data.get("code") != 0:
                    raise RuntimeError(
                        f"tenant_access_token 获取失败: {data.get('msg')}"
                    )
                self._token = data["tenant_access_token"]
                self._token_expire = time.time() + data.get("expire", 7200)
                return self._token

    async def download_message_resource(
        self, message_id: str, file_key: str, resource_type: str
    ) -> bytes | None:
        """resource_type: image（图片）| file（视频/音频/附件）。"""
        token = await self.tenant_token()
        url = f"{API_BASE}/im/v1/messages/{message_id}/resources/{file_key}"
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120)
        ) as sess, sess.get(
            url, params={"type": resource_type}, headers={"Authorization": f"Bearer {token}"}
        ) as resp:
            if resp.status != 200:
                body = (await resp.text())[:200]
                print(f"[feishu] 资源下载失败 {resource_type} {file_key}: "
                      f"HTTP {resp.status} {body}")
                return None
            return await resp.read()

    async def reply_text(self, message_id: str, text: str) -> None:
        """回复指定消息（文本）。超长自动截断（飞书单条上限约 150KB）。"""
        token = await self.tenant_token()
        payload = {
            "msg_type": "text",
            "content": _json_dumps({"text": text[:140000]}),
        }
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as sess, sess.post(
            f"{API_BASE}/im/v1/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        ) as resp:
            data = await resp.json()
            if data.get("code") != 0:
                print(f"[feishu] 回复失败: {data.get('msg')}")


def _json_dumps(obj: dict) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
