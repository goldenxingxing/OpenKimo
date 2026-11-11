"""Inline terminal image preview.

Displays an image directly in the terminal using the emulator's graphics
protocol, aligned with kimo-cli's TUI image display:

- **iTerm2** (also WezTerm / mintty): OSC 1337 ``File`` inline image.
- **Kitty**: the Kitty graphics protocol (APC ``G``), PNG transmitted in chunks.

Protocol support is detected from the environment. When the terminal cannot
display images (or the format is unsupported) the caller should fall back to
showing the file path — :func:`display_image` returns ``False`` in that case and
never writes a partial escape sequence.

This module is pure I/O-free until :func:`display_image` is called; detection
and encoding are side-effect free and unit-tested. The experimental gate is
enforced by the caller (the ``/image`` slash command) via
``kimo_cli.flags.enabled("image_preview")``.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Literal

from kimo_cli.flags import register_flag

ImageProtocol = Literal["iterm2", "kitty"]

register_flag(
    "image_preview",
    description="Inline terminal image preview (/image) via iTerm2/Kitty graphics.",
    default=False,
)

# PNG magic; Kitty's protocol requires PNG (f=100) for direct transmission.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def detect_protocol(env: Mapping[str, str] | None = None) -> ImageProtocol | None:
    """Detect the terminal's inline-image protocol, or ``None`` if unsupported.

    Kitty is checked first because it sets both ``TERM`` and ``KITTY_WINDOW_ID``
    and its protocol is preferred where available.
    """
    env = os.environ if env is None else env

    term = env.get("TERM", "")
    term_program = env.get("TERM_PROGRAM", "")

    if env.get("KITTY_WINDOW_ID") or "kitty" in term:
        return "kitty"
    if term_program in {"iTerm.app", "WezTerm"}:
        return "iterm2"
    # WezTerm also exports TERM_PROGRAM=WezTerm; some terminals only set TERM.
    if "wezterm" in term.lower():
        return "iterm2"
    return None


def supports_images(env: Mapping[str, str] | None = None) -> bool:
    return detect_protocol(env) is not None


def encode_iterm2(
    data: bytes,
    *,
    name: str = "image",
    width: str = "auto",
    height: str = "auto",
) -> str:
    """Encode an iTerm2 OSC 1337 inline-image escape sequence."""
    b64_name = base64.b64encode(name.encode("utf-8", "replace")).decode("ascii")
    b64_data = base64.b64encode(data).decode("ascii")
    return (
        f"\x1b]1337;File=name={b64_name};size={len(data)};"
        f"width={width};height={height};inline=1:{b64_data}\x07"
    )


def encode_kitty(data: bytes, *, chunk_size: int = 4096) -> str:
    """Encode a Kitty graphics-protocol transmission for PNG ``data``.

    PNG (``f=100``) is sent directly (``t=d``) with no compression, split into
    ``chunk_size`` base64 chunks using the ``m`` continuation flag.
    """
    if not data.startswith(_PNG_MAGIC):
        raise ValueError("Kitty inline preview requires PNG data")
    b64 = base64.b64encode(data).decode("ascii")
    chunks = [b64[i : i + chunk_size] for i in range(0, len(b64), chunk_size)] or [""]
    parts: list[str] = []
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        control = "a=T,f=100,t=d" if index == 0 else ""
        m = 0 if last else 1
        parts.append(f"\x1b_G{control}{';' if control else ''}m={m};{chunk}\x1b\\")
    return "".join(parts)


def encode_image(
    path: Path,
    protocol: ImageProtocol,
    *,
    width: str = "auto",
    height: str = "auto",
) -> str:
    """Read ``path`` and encode it for ``protocol``. Raises on unsupported data."""
    data = Path(path).read_bytes()
    if protocol == "iterm2":
        return encode_iterm2(data, name=Path(path).name, width=width, height=height)
    if protocol == "kitty":
        return encode_kitty(data)
    raise ValueError(f"Unsupported protocol: {protocol}")


def display_image(
    path: Path,
    *,
    out: IO[str] | None = None,
    env: Mapping[str, str] | None = None,
    width: str = "auto",
    height: str = "auto",
) -> bool:
    """Display ``path`` inline. Returns ``True`` on success, ``False`` on fallback.

    On fallback nothing is written to ``out``; the caller should print the path.
    """
    import sys

    protocol = detect_protocol(env)
    if protocol is None:
        return False
    try:
        escape = encode_image(Path(path), protocol, width=width, height=height)
    except (OSError, ValueError):
        return False
    (sys.stdout if out is None else out).write(escape + "\n")
    (sys.stdout if out is None else out).flush()
    return True
