from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from kimo_cli.utils.logging import logger

from .models import is_terminal_status
from .store import BackgroundTaskStore

INTERRUPTED_NOTIFICATION_TYPES = frozenset({"task.failed", "task.killed", "task.lost"})


def background_status_counts(
    session_dir: Path, *, stale_after_s: float = 15.0, now: float | None = None
) -> tuple[int, int]:
    """Count (live, interrupted-unseen) background tasks for a session.

    Reads on-disk state only, so it also works for sessions whose process is
    gone. Never raises: missing directories, corrupt JSON, and missing fields
    all count as nothing.
    """
    if now is None:
        now = time.time()
    live = _live_task_count(session_dir / "tasks", stale_after_s=stale_after_s, now=now)
    interrupted = _unseen_interrupted_count(session_dir / "notifications")
    return live, interrupted


def _live_task_count(tasks_dir: Path, *, stale_after_s: float, now: float) -> int:
    try:
        views = BackgroundTaskStore(tasks_dir).list_views()
    except Exception as exc:  # defensive: a broken store must not break listing
        logger.warning(
            "Failed to list background tasks from {path}: {error}", path=tasks_dir, error=exc
        )
        return 0
    count = 0
    for view in views:
        if is_terminal_status(view.runtime.status):
            continue
        last_progress_at = (
            view.runtime.heartbeat_at
            or view.runtime.started_at
            or view.runtime.updated_at
            or view.spec.created_at
        )
        # A stale non-terminal task is a worker that died with the machine;
        # it must not read as "running" in the session list.
        if now - last_progress_at <= stale_after_s:
            count += 1
    return count


def _unseen_interrupted_count(notifications_dir: Path) -> int:
    if not notifications_dir.is_dir():
        return 0
    count = 0
    for path in sorted(notifications_dir.iterdir()):
        if not path.is_dir():
            continue
        try:
            event = json.loads((path / "event.json").read_text(encoding="utf-8"))
            delivery = json.loads((path / "delivery.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict) or not isinstance(delivery, dict):
            continue
        if event.get("type") not in INTERRUPTED_NOTIFICATION_TYPES:
            continue
        # The wire sink acks once the user has seen the notification in the UI.
        if _sink_status(delivery, "wire") != "acked":
            count += 1
    return count


def _sink_status(delivery: dict[str, Any], sink: str) -> str | None:
    sinks = delivery.get("sinks")
    if not isinstance(sinks, dict):
        return None
    state = sinks.get(sink)
    if not isinstance(state, dict):
        return None
    status = state.get("status")
    return status if isinstance(status, str) else None
