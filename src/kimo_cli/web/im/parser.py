"""Feishu message parser: convert msg_type payloads into OpenKimo user_input.

Ported from integrations/feishu-bridge (QLi, 2026-11-18, production-tested).
Hard-won specifics kept:

- New-style Feishu video/file messages are ``post`` type with attachments in
  the top-level ``files`` array of the content; the body content only has text.
  Parsing content tags alone silently drops attachments.
- Image messages (image): content.image_key, download with type=image.
- Video/audio/attachments: content.file_key, download with type=file.
- LLM APIs cannot load local-path image_url values; large media is persisted
  under ``media_dir`` and referenced by path in a text part instead.

Output policy:
- text             -> text part
- image (small)    -> image_url data URI (model reads it directly)
- image (large) / video / audio / file -> saved to media_dir, path + tool hints in text
"""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any

from kimo_cli.web.im.feishu_api import FeishuAPI

# base64 超过该长度（约 1.9MB 原图）不内联，改存盘。
_MAX_B64_INLINE = 2_500_000

_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}
_AUDIO_SUFFIXES = {".wav", ".mp3", ".opus", ".m4a", ".aac", ".ogg"}


def _sniff_image_mime(b64: str) -> str:
    try:
        head = base64.b64decode(b64[:32] + "==")
    except Exception:
        return "image/jpeg"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head.startswith(b"RIFF") and b"WEBP" in head[:16]:
        return "image/webp"
    return "image/jpeg"


class MessageParser:
    def __init__(self, api: FeishuAPI, media_dir: Path) -> None:
        self.api = api
        self.media_dir = media_dir
        self.media_dir.mkdir(parents=True, exist_ok=True)

    async def parse(self, message_id: str, msg_type: str, content_raw: str) -> list[dict[str, Any]]:
        try:
            content = json.loads(content_raw) if content_raw else {}
        except json.JSONDecodeError:
            return [{"type": "text", "text": content_raw}]

        parts: list[dict[str, Any]] = []
        text_bits: list[str] = []

        if msg_type == "text":
            text_bits.append(str(content.get("text", "")))
        elif msg_type == "image":
            await self._handle_image(
                message_id, str(content.get("image_key", "")), parts, text_bits
            )
        elif msg_type == "media":
            await self._handle_attachment(
                message_id,
                str(content.get("file_key", "")),
                str(content.get("file_name", "")) or "video.mp4",
                parts,
                text_bits,
            )
        elif msg_type == "file":
            await self._handle_attachment(
                message_id,
                str(content.get("file_key", "")),
                str(content.get("file_name", "")) or "file.bin",
                parts,
                text_bits,
            )
        elif msg_type == "audio":
            await self._handle_attachment(
                message_id,
                str(content.get("file_key", "")),
                str(content.get("file_name", "")) or "audio.opus",
                parts,
                text_bits,
            )
        elif msg_type == "post":
            self._parse_post_text(content, text_bits)
            # 正文内嵌图片/视频（老结构）
            for seg in self._iter_post_tags(content):
                tag = seg.get("tag")
                if tag == "img":
                    await self._handle_image(
                        message_id, str(seg.get("image_key", "")), parts, text_bits
                    )
            # 顶层 files 附件（新结构，视频/文件都在这里）——核心修复
            for f in content.get("files") or []:
                if not isinstance(f, dict):
                    continue
                await self._handle_attachment(
                    message_id,
                    str(f.get("file_key", "")),
                    str(f.get("file_name", "")) or "file.bin",
                    parts,
                    text_bits,
                )
        else:
            text_bits.append(f"[不支持的消息类型: {msg_type}]")

        text = "\n".join(t for t in text_bits if t and t.strip()).strip()
        if text:
            parts.insert(0, {"type": "text", "text": text})
        return parts

    # ------------------------------------------------------------------ #
    def _iter_post_tags(self, content: dict) -> Any:
        for para in content.get("content") or []:
            items = para if isinstance(para, list) else [para]
            for seg in items:
                if isinstance(seg, dict):
                    yield seg

    def _parse_post_text(self, content: dict, text_bits: list[str]) -> None:
        title = str(content.get("title") or "").strip()
        if title:
            text_bits.append(title)
        for para in content.get("content") or []:
            items = para if isinstance(para, list) else [para]
            line = ""
            for seg in items:
                if not isinstance(seg, dict):
                    continue
                tag = seg.get("tag")
                if tag == "text":
                    line += str(seg.get("text", ""))
                elif tag == "a":
                    href = str(seg.get("href", "")).strip()
                    t = str(seg.get("text", ""))
                    line += f"{t}({href})" if href else t
                elif tag == "at":
                    line += str(seg.get("name", ""))
            if line.strip():
                text_bits.append(line)

    # ------------------------------------------------------------------ #
    async def _handle_image(
        self,
        message_id: str,
        image_key: str,
        parts: list[dict],
        text_bits: list[str],
    ) -> None:
        if not image_key:
            return
        data = await self.api.download_message_resource(message_id, image_key, "image")
        if not data:
            text_bits.append("[图片下载失败]")
            return
        b64 = base64.b64encode(data).decode("ascii")
        if len(b64) <= _MAX_B64_INLINE:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{_sniff_image_mime(b64)};base64,{b64}"},
                }
            )
        else:
            path = self._save(data, "image.jpg")
            text_bits.append(
                f"[用户发送了一张图片，已保存到: {path}，请用 ReadMediaFile 工具查看内容]"
            )

    async def _handle_attachment(
        self,
        message_id: str,
        file_key: str,
        file_name: str,
        parts: list[dict],
        text_bits: list[str],
    ) -> None:
        if not file_key:
            return
        data = await self.api.download_message_resource(message_id, file_key, "file")
        if not data:
            text_bits.append(f"[附件 {file_name} 下载失败]")
            return
        path = self._save(data, file_name)
        suffix = Path(file_name).suffix.lower()
        if suffix in _VIDEO_SUFFIXES:
            text_bits.append(
                f"[用户发送了一个视频，已保存到: {path}，"
                "请用 ffmpeg 抽取关键帧保存为图片后，"
                "用 ReadMediaFile 逐帧查看并回答用户问题]"
            )
        elif suffix in _AUDIO_SUFFIXES:
            text_bits.append(f"[用户发送了一段语音，已保存到: {path}，请用工具转文字或分析后回答]")
        else:
            text_bits.append(
                f"[用户发送了附件文件 {file_name}，已保存到: {path}，请用工具读取内容后回答]"
            )

    def _save(self, data: bytes, name: str) -> str:
        safe = "".join(c for c in name if c.isalnum() or c in ".-_")[:60] or "file"
        path = self.media_dir / f"{uuid.uuid4().hex[:8]}_{safe}"
        path.write_bytes(data)
        return str(path)
