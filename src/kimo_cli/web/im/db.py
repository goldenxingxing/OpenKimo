"""Persistence for IM bridge: channels, whitelists, bindings.

Tables live in the shared ``users.db`` (same SQLite as user management), following
the DDL-string + init + crud conventions of ``kimo_cli.web.db.database``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, cast

from kimo_cli.web.db.database import db_session

_CREATE_IM_CHANNELS_TABLE = """
CREATE TABLE IF NOT EXISTS im_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    app_id TEXT NOT NULL DEFAULT '',
    app_secret TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0,
    config TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

_CREATE_IM_WHITELIST_TABLE = """
CREATE TABLE IF NOT EXISTS im_whitelist (
    channel_id INTEGER NOT NULL,
    external_id TEXT NOT NULL,
    PRIMARY KEY (channel_id, external_id)
);
"""

_CREATE_IM_BINDINGS_TABLE = """
CREATE TABLE IF NOT EXISTS im_bindings (
    channel_id INTEGER NOT NULL,
    external_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (channel_id, external_id)
);
"""

_CREATE_IM_PENDING_TABLE = """
CREATE TABLE IF NOT EXISTS im_pending (
    channel_id INTEGER NOT NULL,
    external_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (channel_id, external_id)
);
"""

_DEFAULT_CHANNEL_CONFIG: dict[str, Any] = {
    "show_thinking": True,
    "group_chat_enabled": False,
    "timeout_seconds": 600,
}


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_IM_CHANNELS_TABLE)
    conn.execute(_CREATE_IM_WHITELIST_TABLE)
    conn.execute(_CREATE_IM_BINDINGS_TABLE)
    conn.execute(_CREATE_IM_PENDING_TABLE)


@dataclass
class ImChannelRow:
    id: int
    type: str
    name: str
    app_id: str
    app_secret: str
    enabled: bool
    config: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ImChannelRow:
        return cls(
            id=row["id"],
            type=row["type"],
            name=row["name"],
            app_id=row["app_id"],
            app_secret=row["app_secret"],
            enabled=bool(row["enabled"]),
            config={**_DEFAULT_CHANNEL_CONFIG, **json.loads(row["config"] or "{}")},
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def list_channels() -> list[ImChannelRow]:
    with db_session() as conn:
        _ensure_tables(conn)
        rows = conn.execute("SELECT * FROM im_channels ORDER BY id").fetchall()
        return [ImChannelRow.from_row(r) for r in rows]


def get_channel(channel_id: int) -> ImChannelRow | None:
    with db_session() as conn:
        _ensure_tables(conn)
        row = conn.execute("SELECT * FROM im_channels WHERE id = ?", (channel_id,)).fetchone()
        return ImChannelRow.from_row(row) if row else None


def create_channel(
    *,
    type: str,
    name: str,
    app_id: str = "",
    app_secret: str = "",
    enabled: bool = False,
    config: dict[str, Any] | None = None,
) -> ImChannelRow:
    now = time.time()
    merged = {**_DEFAULT_CHANNEL_CONFIG, **(config or {})}
    with db_session() as conn:
        _ensure_tables(conn)
        cur = conn.execute(
            "INSERT INTO im_channels (type, name, app_id, app_secret, enabled,"
            " config, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (type, name, app_id, app_secret, int(enabled), json.dumps(merged), now, now),
        )
        channel_id = cast(int, cur.lastrowid)
    row = get_channel(channel_id)
    assert row is not None
    return row


def update_channel(
    channel_id: int,
    *,
    name: str | None = None,
    app_id: str | None = None,
    app_secret: str | None = None,
    enabled: bool | None = None,
    config: dict[str, Any] | None = None,
) -> ImChannelRow | None:
    existing = get_channel(channel_id)
    if existing is None:
        return None
    merged = {**existing.config, **(config or {})}
    with db_session() as conn:
        _ensure_tables(conn)
        conn.execute(
            "UPDATE im_channels SET name = ?, app_id = ?, app_secret = ?,"
            " enabled = ?, config = ?, updated_at = ? WHERE id = ?",
            (
                existing.name if name is None else name,
                existing.app_id if app_id is None else app_id,
                existing.app_secret if app_secret is None else app_secret,
                existing.enabled if enabled is None else int(enabled),
                json.dumps(merged),
                time.time(),
                channel_id,
            ),
        )
    return get_channel(channel_id)


def delete_channel(channel_id: int) -> None:
    with db_session() as conn:
        _ensure_tables(conn)
        conn.execute("DELETE FROM im_channels WHERE id = ?", (channel_id,))
        conn.execute("DELETE FROM im_whitelist WHERE channel_id = ?", (channel_id,))
        conn.execute("DELETE FROM im_bindings WHERE channel_id = ?", (channel_id,))
        conn.execute("DELETE FROM im_pending WHERE channel_id = ?", (channel_id,))


def get_whitelist(channel_id: int) -> list[str]:
    with db_session() as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            "SELECT external_id FROM im_whitelist WHERE channel_id = ? ORDER BY external_id",
            (channel_id,),
        ).fetchall()
        return [r["external_id"] for r in rows]


def set_whitelist(channel_id: int, external_ids: list[str]) -> None:
    cleaned = [e for e in (eid.strip() for eid in external_ids) if e]
    with db_session() as conn:
        _ensure_tables(conn)
        conn.execute("DELETE FROM im_whitelist WHERE channel_id = ?", (channel_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO im_whitelist (channel_id, external_id) VALUES (?, ?)",
            [(channel_id, eid) for eid in dict.fromkeys(cleaned)],
        )


def add_whitelist(channel_id: int, external_id: str) -> None:
    external_id = external_id.strip()
    if not external_id:
        return
    with db_session() as conn:
        _ensure_tables(conn)
        conn.execute(
            "INSERT OR IGNORE INTO im_whitelist (channel_id, external_id) VALUES (?, ?)",
            (channel_id, external_id),
        )


def is_whitelisted(channel_id: int, external_id: str) -> bool:
    with db_session() as conn:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT 1 FROM im_whitelist WHERE channel_id = ? AND external_id = ?",
            (channel_id, external_id),
        ).fetchone()
        return row is not None


def get_binding(channel_id: int, external_id: str) -> str | None:
    """Return the bound session id, or None when unbound."""
    with db_session() as conn:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT session_id FROM im_bindings WHERE channel_id = ? AND external_id = ?",
            (channel_id, external_id),
        ).fetchone()
        return row["session_id"] if row else None


def set_binding(channel_id: int, external_id: str, session_id: str, display_name: str = "") -> None:
    with db_session() as conn:
        _ensure_tables(conn)
        conn.execute(
            "INSERT INTO im_bindings (channel_id, external_id, session_id, display_name,"
            " updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(channel_id, external_id)"
            " DO UPDATE SET session_id = excluded.session_id,"
            " display_name = excluded.display_name, updated_at = excluded.updated_at",
            (channel_id, external_id, session_id, display_name, time.time()),
        )


def drop_binding(channel_id: int, external_id: str) -> None:
    with db_session() as conn:
        _ensure_tables(conn)
        conn.execute(
            "DELETE FROM im_bindings WHERE channel_id = ? AND external_id = ?",
            (channel_id, external_id),
        )


def list_bindings(channel_id: int) -> list[dict[str, Any]]:
    with db_session() as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            "SELECT external_id, display_name, session_id FROM im_bindings"
            " WHERE channel_id = ? ORDER BY updated_at DESC",
            (channel_id,),
        ).fetchall()
        return [
            {
                "external_id": r["external_id"],
                "display_name": r["display_name"],
                "session_id": r["session_id"],
            }
            for r in rows
        ]


# ---------------------------------------------------------------------- #
# Pending senders: strangers who messaged the bot and await authorization.
# The web UI shows this list with one-click approve — no manual open_id hunt.
# ---------------------------------------------------------------------- #
def record_pending(channel_id: int, external_id: str, display_name: str = "") -> None:
    now = time.time()
    with db_session() as conn:
        _ensure_tables(conn)
        conn.execute(
            "INSERT INTO im_pending (channel_id, external_id, display_name,"
            " first_seen, last_seen, message_count) VALUES (?, ?, ?, ?, ?, 1)"
            " ON CONFLICT(channel_id, external_id) DO UPDATE SET"
            " last_seen = excluded.last_seen,"
            " message_count = im_pending.message_count + 1,"
            " display_name = CASE WHEN excluded.display_name != ''"
            " THEN excluded.display_name ELSE im_pending.display_name END",
            (channel_id, external_id, display_name, now, now),
        )
        # Keep only the newest three per channel: the UI shows a short
        # actionable list, not a log of every stranger ever.
        conn.execute(
            "DELETE FROM im_pending WHERE channel_id = ? AND external_id NOT IN ("
            " SELECT external_id FROM im_pending WHERE channel_id = ?"
            " ORDER BY last_seen DESC LIMIT 3)",
            (channel_id, channel_id),
        )


def list_pending(channel_id: int) -> list[dict[str, Any]]:
    with db_session() as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            "SELECT external_id, display_name, last_seen, message_count"
            " FROM im_pending WHERE channel_id = ? ORDER BY last_seen DESC",
            (channel_id,),
        ).fetchall()
        return [
            {
                "external_id": r["external_id"],
                "display_name": r["display_name"],
                "last_seen": r["last_seen"],
                "message_count": r["message_count"],
            }
            for r in rows
        ]


def delete_pending(channel_id: int, external_id: str) -> None:
    with db_session() as conn:
        _ensure_tables(conn)
        conn.execute(
            "DELETE FROM im_pending WHERE channel_id = ? AND external_id = ?",
            (channel_id, external_id),
        )
