"""Feishu OpenAPI client: tenant token management + message resource download.

Ported from integrations/feishu-bridge (QLi, 2026-11-18, production-tested) with
print() replaced by the shared logger. Hard-won specifics kept:

- Message resource download: GET /im/v1/messages/{id}/resources/{file_key}?type=image|file
  (images use type=image; video/audio/attachments use type=file).
- The app needs im:message + im:resource scopes and im.message.receive_v1 subscription.
"""

from __future__ import annotations

import asyncio
import json
import time

import aiohttp

from kimo_cli.utils.logging import logger

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
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as sess,
                sess.post(
                    f"{API_BASE}/auth/v3/tenant_access_token/internal",
                    json={
                        "app_id": self.app_id,
                        "app_secret": self.app_secret,
                    },
                ) as resp,
            ):
                data = await resp.json()
                if data.get("code") != 0:
                    raise RuntimeError(f"tenant_access_token 获取失败: {data.get('msg')}")
                self._token = data["tenant_access_token"]
                self._token_expire = time.time() + data.get("expire", 7200)
                return self._token

    async def download_message_resource(
        self, message_id: str, file_key: str, resource_type: str
    ) -> bytes | None:
        """resource_type: image（图片）| file（视频/音频/附件）。"""
        token = await self.tenant_token()
        url = f"{API_BASE}/im/v1/messages/{message_id}/resources/{file_key}"
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as sess,
            sess.get(
                url, params={"type": resource_type}, headers={"Authorization": f"Bearer {token}"}
            ) as resp,
        ):
            if resp.status != 200:
                body = (await resp.text())[:200]
                logger.warning(
                    "Feishu resource download failed {type} {key}: HTTP {status} {body}",
                    type=resource_type,
                    key=file_key,
                    status=resp.status,
                    body=body,
                )
                return None
            return await resp.read()

    async def reply_text(self, message_id: str, text: str) -> None:
        """回复指定消息（文本）。超长自动截断（飞书单条上限约 150KB）。"""
        token = await self.tenant_token()
        payload = {
            "msg_type": "text",
            "content": json.dumps({"text": text[:140000]}, ensure_ascii=False),
        }
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as sess,
            sess.post(
                f"{API_BASE}/im/v1/messages/{message_id}/reply",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            ) as resp,
        ):
            data = await resp.json()
            if data.get("code") != 0:
                logger.warning("Feishu reply failed: {msg}", msg=data.get("msg"))
