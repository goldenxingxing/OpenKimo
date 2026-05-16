"""Re-export shim for the macOS dotenv_io module (pure-Python, OS-agnostic)."""

from __future__ import annotations

from packaging.app_main.dotenv_io import (  # noqa: F401
    EDITABLE_KEYS,
    SECRET_KEYS,
    load_into_environ,
    mask_secret,
    read_editable,
    read_env,
    write_env,
)

__all__ = [
    "EDITABLE_KEYS",
    "SECRET_KEYS",
    "load_into_environ",
    "mask_secret",
    "read_editable",
    "read_env",
    "write_env",
]
