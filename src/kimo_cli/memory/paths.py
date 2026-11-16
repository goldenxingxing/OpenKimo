from __future__ import annotations

import os
from pathlib import Path

ANONYMOUS_USER_SENTINEL = "__anonymous__"
_USER_MEMORY_FILENAME = "persistent.jsonl"


def resolve_owner_id(owner_id: str | None) -> str:
    """Map ``owner_id`` (possibly ``None``) to a filesystem-safe directory name.

    Returns the sentinel ``__anonymous__`` for sessions without an authenticated
    owner so legacy data does not pollute real users' memory.

    Rejects traversal sequences (``..``, ``/``, ``\\``) to prevent escaping
    the users/ directory.
    """
    if not owner_id:
        return ANONYMOUS_USER_SENTINEL
    if (
        not owner_id.strip()
        or owner_id in {".", ".."}
        or "/" in owner_id
        or "\\" in owner_id
        or Path(owner_id).name != owner_id
    ):
        raise ValueError(f"Invalid owner_id (not a plain directory name): {owner_id!r}")
    return owner_id


def _share_root() -> Path:
    """Return the host-mounted shared volume root.

    Falls back to ``~/.kimo/share`` outside the sandbox so the same code paths
    work in local-mode CLI usage.
    """
    env = os.environ.get("KIMO_SHARE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".kimo" / "share"


def get_user_memory_dir(owner_id: str | None) -> Path:
    """Return the user-private memory directory.

    Layout: ``{KIMO_SHARE_DIR}/users/{owner_id}/memory/``.
    """
    return _share_root() / "users" / resolve_owner_id(owner_id) / "memory"


def get_persistent_memory_file(owner_id: str | None) -> Path:
    return get_user_memory_dir(owner_id) / _USER_MEMORY_FILENAME
