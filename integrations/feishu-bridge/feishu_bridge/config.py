"""配置加载：config.json（不入库），缺失字段用默认值。"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULTS: dict = {
    "feishu_app_id": "",
    "feishu_app_secret": "",
    "openkimo_base_url": "http://127.0.0.1:5494",
    "openkimo_token": "",
    "data_dir": "data",
    "timeout_seconds": 600,
    "show_thinking": True,
    "group_chat_enabled": False,
}


def load_config(base_dir: Path) -> dict:
    cfg = dict(DEFAULTS)
    path = base_dir / "config.json"
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception as e:
            raise SystemExit(f"配置文件解析失败 {path}: {e}")
    if not cfg["feishu_app_id"] or not cfg["feishu_app_secret"]:
        raise SystemExit(
            f"请先在 {path} 填入 feishu_app_id / feishu_app_secret"
            f"（可复制 config.example.json 改名）"
        )
    return cfg
