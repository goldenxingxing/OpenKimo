from __future__ import annotations

import os
import subprocess
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, cast

NAME = "OpenKimo"

if TYPE_CHECKING:
    VERSION: str
    USER_AGENT: str

# Build SHA injected at package/build time via scripts/inject_build_sha.py.
# When present it overrides any runtime detection.


#: The kimo-cli release this client identifies as to anything outside it.
#:
#: OpenKimo carries on from MoonshotAI's kimo-cli 1.49.0 (the upstream project
#: is archived) and numbers its own releases, which start again from 0.x. The
#: services this client talks to know it by the upstream name and number: the
#: Kimo API reads `User-Agent: KimoCLI/<version>`, the OAuth endpoints read
#: `X-Msh-Version`, telemetry and plugins are told a host version. Sending
#: OpenKimo's own number there would present a "0.3.0" client to services that
#: have only ever seen 1.x, so every outward identity stays pinned here while
#: `get_version()` reports the version a person installed.
COMPAT_VERSION = "1.49.0"


@cache
def get_version() -> str:
    """The OpenKimo version installed, as shown to the person using it."""
    from importlib import metadata

    return metadata.version("kimo-cli")


@cache
def get_user_agent() -> str:
    return f"KimoCLI/{COMPAT_VERSION}"


def _normalize_remote(url: str) -> str:
    """Normalize a git remote URL to host/path format."""
    url = url.strip()
    for prefix in ("git@", "https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix) :]
    if "@" in url:
        url = url.split("@", 1)[1]
    if url.endswith(".git"):
        url = url[:-4]
    if ":" in url and "/" not in url.split(":", 1)[0]:
        url = url.replace(":", "/", 1)
    return url


@cache
def get_build_sha() -> str:
    """Return the build identifier of this build.

    Format: ``remote@sha`` when remote is available, otherwise just ``sha``.

    Priority:
    1. KIMO_BUILD_SHA environment variable (dev / CI override)
    2. Hardcoded BUILD_SHA from _build_info.py (set by wheel / PyInstaller build)
    3. git remote + git rev-parse HEAD from the package directory (dev mode)
    4. Empty string (fallback)
    """
    if build_id := os.environ.get("KIMO_BUILD_SHA", "").strip():
        return build_id

    try:
        from kimo_cli._build_info import BUILD_SHA  # type: ignore[reportMissingImports]

        return cast(str, BUILD_SHA)
    except ImportError:
        pass

    sha = ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()[:12]
    except Exception:
        pass

    remote = ""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            remote = _normalize_remote(result.stdout)
    except Exception:
        pass

    if remote and sha:
        return f"{remote}@{sha}"
    return sha


def __getattr__(name: str) -> str:
    if name == "VERSION":
        # Outward identity (OAuth headers, telemetry, plugin host version).
        return COMPAT_VERSION
    if name == "USER_AGENT":
        return get_user_agent()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "COMPAT_VERSION",
    "NAME",
    "VERSION",
    "USER_AGENT",
    "get_version",
    "get_user_agent",
    "get_build_sha",
]
