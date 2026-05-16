"""Re-export shim: reuse the macOS uvicorn supervisor verbatim.

The supervisor is platform-neutral (subprocess + threading + signal.SIGTERM)
so we import it from the sibling ``app_main`` package. Importing through
``from . import server`` keeps the Windows entry-point's import graph local.
"""

from __future__ import annotations

from packaging.app_main.server import (  # noqa: F401
    EXIT_CODE_RESTART,
    GRACEFUL_KILL_TIMEOUT,
    HEALTHCHECK_DEADLINE,
    MAX_AUTO_RESTARTS,
    ServerState,
    UvicornSupervisor,
)

__all__ = [
    "EXIT_CODE_RESTART",
    "GRACEFUL_KILL_TIMEOUT",
    "HEALTHCHECK_DEADLINE",
    "MAX_AUTO_RESTARTS",
    "ServerState",
    "UvicornSupervisor",
]
