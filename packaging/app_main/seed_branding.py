"""Seed Web UI branding (logo / brand_name / version / …) from the bundle.

The build step embeds a ``branding_seed`` block inside
``Resources/brand.json``. On every launch we reconcile per-field:

* User-facing fields (``brand_name``, ``logo``, ``page_title``, ``favicon``)
  are seeded only when the DB row is empty, so admin-panel customisations
  survive upgrades.
* Build-derived fields (``version``) are re-synced to the packaged value
  every launch, so upgrading the .app refreshes the displayed version and
  installs that predate a field get it populated retroactively.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from .paths import AppPaths

log = logging.getLogger(__name__)

_BRANDING_FIELDS = ("brand_name", "version", "page_title", "logo", "favicon")

# Build-derived fields the user cannot meaningfully customise via the admin
# panel. We re-sync these to the packaged value on every launch so upgrading
# the .app updates the displayed version, and so installs that predate the
# field (e.g. <= v0.1.5) get it populated retroactively.
_BUILD_DERIVED_FIELDS = ("version",)


def _ensure_kimi_cli_on_path(p: AppPaths) -> None:
    parent = str(p.kimi_cli.parent)
    if p.kimi_cli.exists() and parent not in sys.path:
        sys.path.insert(0, parent)


def _load_seed(p: AppPaths) -> dict[str, str] | None:
    if not p.brand_json.exists():
        return None
    try:
        data = json.loads(p.brand_json.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("brand.json unreadable: %s", e)
        return None
    seed = data.get("branding_seed")
    if not isinstance(seed, dict):
        return None
    return {k: v for k, v in seed.items() if k in _BRANDING_FIELDS and v}


def seed_if_needed(p: AppPaths) -> None:
    """Idempotent seed; safe to call on every launch."""
    seed = _load_seed(p)
    if not seed:
        return

    os.environ.setdefault("KIMI_SHARE_DIR", str(p.sessions_dir))
    _ensure_kimi_cli_on_path(p)

    try:
        from kimi_cli.web.db.crud import get_branding, upsert_branding
        from kimi_cli.web.db.database import get_db, init_db
    except ImportError as e:
        log.warning("kimi_cli not importable, skipping branding seed: %s", e)
        return

    try:
        init_db()
        with get_db() as db:
            existing = get_branding(db)
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
            upsert_branding(db, to_write)
            log.info("seeded branding from brand.json (%s)", ", ".join(to_write))
    except Exception:
        log.exception("branding seed failed")


def reset_to_packaged(p: AppPaths) -> None:
    """Wipe branding rows and re-apply the packaged seed (Settings button)."""
    seed = _load_seed(p)
    os.environ.setdefault("KIMI_SHARE_DIR", str(p.sessions_dir))
    _ensure_kimi_cli_on_path(p)

    try:
        from kimi_cli.web.db.crud import delete_all_branding, upsert_branding
        from kimi_cli.web.db.database import get_db, init_db
    except ImportError as e:
        log.warning("kimi_cli not importable, branding reset skipped: %s", e)
        return

    init_db()
    with get_db() as db:
        delete_all_branding(db)
        if seed:
            upsert_branding(db, seed)
