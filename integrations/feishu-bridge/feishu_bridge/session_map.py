"""飞书用户 open_id -> OpenKimo session_id 映射持久化。"""
from __future__ import annotations

import json
import threading
from pathlib import Path


class SessionMap:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._map: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._map = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._map = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._map, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[session_map] persist failed: {e}")

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._map.get(key)

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._map[key] = value
            self._save()

    def drop(self, key: str) -> None:
        with self._lock:
            if key in self._map:
                del self._map[key]
                self._save()
