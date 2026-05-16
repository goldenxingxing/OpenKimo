"""Re-export shim for the macOS seed_branding module (pure-Python, OS-agnostic)."""

from __future__ import annotations

from packaging.app_main.seed_branding import (  # noqa: F401
    reset_to_packaged,
    seed_if_needed,
)

__all__ = ["reset_to_packaged", "seed_if_needed"]
