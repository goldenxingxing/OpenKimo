"""User-writable Python environment overlay — Windows variant.

The macOS variant materialises a userbase directory, writes ``pip.conf``,
and drops POSIX shell shims into ``userbase/bin``. On Windows we lean on
``pip install --user``'s built-in default which is ``%APPDATA%\\Python``;
there's no need for a custom userbase, no need for shims (PATH-prepended
``Scripts\\`` works directly), and no first-launch bootstrap.

This module exists so the supervisor can keep calling
``userenv.env_overlay(paths)`` unchanged — it just returns ``{}`` here.
"""

from __future__ import annotations

from .paths import AppPaths


def setup(p: AppPaths) -> None:  # noqa: ARG001 - signature parity
    """No-op on Windows; ``pip install --user`` already targets ``%APPDATA%\\Python``."""
    return


def env_overlay(p: AppPaths) -> dict[str, str]:  # noqa: ARG001 - signature parity
    """Return an empty overlay — Windows ``pip install --user`` needs no PYTHONUSERBASE override."""
    return {}


def reset(p: AppPaths) -> None:  # noqa: ARG001 - signature parity
    """No-op on Windows; reserved for parity with the macOS reset menu."""
    return
