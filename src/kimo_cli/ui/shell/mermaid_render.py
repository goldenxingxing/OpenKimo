"""Render Mermaid diagrams for the terminal.

kimo-cli renders ``mermaid`` code fences as diagrams in its TUI. This module
provides the equivalent capability for the shell:

1. Render the Mermaid source to a PNG via ``mmdc`` (mermaid-cli), when installed.
2. Display the PNG inline using :mod:`kimo_cli.ui.shell.image_preview`.

``mmdc`` is optional and detected at runtime. When it is missing, or rendering
fails, or the terminal cannot show images, the caller falls back to showing the
code block (plus a hint). The experimental gate is enforced by the caller (the
``/mermaid`` slash command) via ``kimo_cli.flags.enabled("mermaid_inline")``.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import IO

from kimo_cli.flags import register_flag

register_flag(
    "mermaid_inline",
    description="Render Mermaid diagrams inline (/mermaid) via mermaid-cli (mmdc).",
    default=False,
)

MMDC_INSTALL_HINT = (
    "Install mermaid-cli to enable diagram rendering: npm install -g @mermaid-js/mermaid-cli"
)


class MermaidRenderError(RuntimeError):
    """Raised when a Mermaid diagram cannot be rendered to an image."""


def find_mmdc() -> str | None:
    """Locate the ``mmdc`` executable, or ``None`` when not installed."""
    return shutil.which("mmdc")


def mmdc_available() -> bool:
    return find_mmdc() is not None


def render_mermaid_to_png(
    code: str,
    *,
    out_path: Path | None = None,
    theme: str = "default",
    timeout_s: float = 60.0,
) -> Path:
    """Render Mermaid ``code`` to a PNG file and return its path.

    Raises :class:`MermaidRenderError` when ``mmdc`` is unavailable or fails.
    """
    if not code.strip():
        raise MermaidRenderError("Empty Mermaid source")
    mmdc = find_mmdc()
    if mmdc is None:
        raise MermaidRenderError(MMDC_INSTALL_HINT)

    if out_path is None:
        out_path = Path(tempfile.mkdtemp(prefix="kimo-mermaid-")) / "diagram.png"

    with tempfile.NamedTemporaryFile(
        "w", suffix=".mmd", prefix="kimo-mermaid-", delete=False, encoding="utf-8"
    ) as src:
        src.write(code)
        src_path = Path(src.name)

    try:
        proc = subprocess.run(
            [
                mmdc,
                "-i",
                str(src_path),
                "-o",
                str(out_path),
                "-t",
                theme,
                "-b",
                "transparent",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MermaidRenderError(f"mmdc failed to run: {exc}") from exc
    finally:
        src_path.unlink(missing_ok=True)

    if proc.returncode != 0 or not out_path.exists():
        detail = (proc.stderr or proc.stdout or "").strip()
        raise MermaidRenderError(f"mmdc exited {proc.returncode}: {detail}")
    return out_path


def render_mermaid_inline(
    code: str,
    *,
    out: IO[str] | None = None,
    theme: str = "default",
) -> bool:
    """Render and inline-display a Mermaid diagram. Returns ``False`` on fallback."""
    from kimo_cli.ui.shell.image_preview import display_image

    try:
        png = render_mermaid_to_png(code, theme=theme)
    except MermaidRenderError:
        return False
    return display_image(png, out=out)
