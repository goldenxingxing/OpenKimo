"""Seed Web UI branding (logo / brand_name / version / …) from the bundle.

The build step embeds a ``branding_seed`` block inside ``brand.json``
(``Resources/`` on macOS, the install root on Windows). On every launch we
reconcile per-field:

* User-facing fields (``brand_name``, ``logo``, ``page_title``, ``favicon``)
  are seeded only when the DB row is empty, so admin-panel customisations
  survive upgrades.
* Build-derived fields (``version``) are re-synced to the packaged value
  every launch, so upgrading the app refreshes the displayed version and
  installs that predate a field get it populated retroactively.

Writes go straight to the ``branding`` key/value table in ``users.db`` via
the stdlib ``sqlite3`` module. The previous approach imported the bundled
``kimi_cli`` package for its CRUD helpers, which dragged kimi-cli's whole
dependency chain into the tray process — any import failure there made the
seed silently no-op (symptom: the web UI shows the upstream "Kimi Code"
defaults instead of the packaged brand).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from . import dotenv_io
from .paths import AppPaths

log = logging.getLogger(__name__)

_BRANDING_FIELDS = ("brand_name", "version", "page_title", "logo", "favicon")

# Build-derived fields the user cannot meaningfully customise via the admin
# panel. We re-sync these to the packaged value on every launch so upgrading
# the app updates the displayed version, and so installs that predate the
# field (e.g. <= v0.1.5) get it populated retroactively.
_BUILD_DERIVED_FIELDS = ("version",)

# Mirrors kimi_cli.web.db.database._CREATE_BRANDING_TABLE. Both sides use
# CREATE TABLE IF NOT EXISTS, so whichever process touches users.db first
# creates it and the other's DDL is a no-op.
_CREATE_BRANDING_TABLE = """
CREATE TABLE IF NOT EXISTS branding (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _db_path(p: AppPaths) -> Path:
    """The users.db the web server will actually read.

    Mirrors server._build_env: our sessions_dir default, unless the user's
    .env overrides KIMI_SHARE_DIR (the .env layer wins there too).
    """
    share = ""
    try:
        share = (dotenv_io.read_env(p.env_file).get("KIMI_SHARE_DIR") or "").strip()
    except Exception:
        log.exception(".env unreadable; using default share dir")
    root = Path(share) if share else p.sessions_dir
    return root / "users.db"


def _load_seed(p: AppPaths) -> dict[str, str] | None:
    if not p.brand_json.exists():
        return None
    try:
        data = json.loads(p.brand_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("brand.json unreadable: %s", e)
        return None
    seed = data.get("branding_seed")
    if not isinstance(seed, dict):
        return None
    return {k: v for k, v in seed.items() if k in _BRANDING_FIELDS and v}


def _connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute(_CREATE_BRANDING_TABLE)
    return conn


def _upsert(conn: sqlite3.Connection, settings: dict[str, str]) -> None:
    conn.executemany(
        "INSERT INTO branding (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        list(settings.items()),
    )


def seed_if_needed(p: AppPaths) -> None:
    """Idempotent seed; safe to call on every launch."""
    seed = _load_seed(p)
    if not seed:
        log.warning("no usable branding_seed in %s; skipping", p.brand_json)
        return

    db = _db_path(p)
    try:
        conn = _connect(db)
    except sqlite3.Error:
        log.exception("branding seed failed opening %s", db)
        return
    try:
        existing = dict(conn.execute("SELECT key, value FROM branding").fetchall())
        to_write: dict[str, str] = {}
        for key, value in seed.items():
            current = existing.get(key)
            if key in _BUILD_DERIVED_FIELDS:
                if current != value:
                    to_write[key] = value
            elif not current:
                to_write[key] = value
        if not to_write:
            return
        _upsert(conn, to_write)
        conn.commit()
        log.info("seeded branding into %s (%s)", db, ", ".join(to_write))
    except sqlite3.Error:
        log.exception("branding seed failed (db=%s)", db)
    finally:
        conn.close()


def reset_to_packaged(p: AppPaths) -> None:
    """Wipe branding rows and re-apply the packaged seed (Settings button)."""
    seed = _load_seed(p)
    conn = _connect(_db_path(p))
    try:
        conn.execute("DELETE FROM branding")
        if seed:
            _upsert(conn, seed)
        conn.commit()
    finally:
        conn.close()
