from __future__ import annotations

import os
import pydoc
import re

from rich.console import Console, PagerContext, RenderableType
from rich.pager import Pager
from rich.theme import Theme

NEUTRAL_MARKDOWN_THEME = Theme(
    {
        "markdown.paragraph": "none",
        "markdown.block_quote": "none",
        "markdown.hr": "none",
        "markdown.list": "none",
        "markdown.item": "none",
        "markdown.item.bullet": "none",
        "markdown.item.number": "none",
        "markdown.link": "bright_blue underline",
        "markdown.link_url": "cyan underline",
        "markdown.h1": "none",
        "markdown.h1.border": "none",
        "markdown.h2": "none",
        "markdown.h3": "none",
        "markdown.h4": "none",
        "markdown.h5": "none",
        "markdown.h6": "none",
        "markdown.h7": "none",
        "markdown.em": "none",
        "markdown.emph": "none",
        "markdown.strong": "none",
        "markdown.s": "none",
        "markdown.code": "none",
        "markdown.code_block": "none",
        "status.spinner": "none",
    },
    inherit=True,
)


class _KimoPager(Pager):
    """Pager that ignores MANPAGER to avoid garbled output.

    ``pydoc.getpager()`` reads ``MANPAGER`` before ``PAGER``.  When the user
    sets ``MANPAGER`` to a man-specific pipeline (e.g.
    ``sh -c 'col -bx | bat -l man -p'``), that pipeline mangles the ANSI
    rich-text we emit.  This pager strips ``MANPAGER`` from the subprocess
    environment so only ``PAGER`` (or the default ``less``) is used.
    """

    def show(self, content: str) -> None:
        saved = os.environ.pop("MANPAGER", None)
        try:
            pydoc.pager(content)
        finally:
            if saved is not None:
                os.environ["MANPAGER"] = saved


class _KimoConsole(Console):
    """Console subclass that defaults to :class:`_KimoPager`."""

    def pager(
        self,
        pager: Pager | None = None,
        styles: bool = False,
        links: bool = False,
    ) -> PagerContext:
        if pager is None:
            pager = _KimoPager()
        return super().pager(pager=pager, styles=styles, links=links)


console = _KimoConsole(highlight=False, theme=NEUTRAL_MARKDOWN_THEME)


# Matches OSC 8 hyperlink open/close markers emitted by Rich's Style(link=...).
# Format: ESC ] 8 ; <params> ; <uri> ST   where ST is ESC \ or BEL (\x07).
# prompt_toolkit's ANSI parser does not understand OSC 8 and renders the raw
# escape bytes as visible garbage (e.g. "8;id=391551;https://…").  We wrap each
# marker in \001…\002 so prompt_toolkit treats it as a ZeroWidthEscape and
# passes it through to the terminal via write_raw, preserving clickable links.
_OSC8_RE = re.compile(r"\x1b\]8;[^\x07\x1b]*(?:\x1b\\|\x07)")


def _wrap_osc8_as_zero_width(m: re.Match[str]) -> str:
    """Wrap an OSC 8 marker in \\001…\\002 for prompt_toolkit ZeroWidthEscape."""
    return f"\x01{m.group(0)}\x02"


def _ansi_color_system() -> str:
    """Resolve the ANSI color system from the environment, platform-independently.

    Rich's own detection special-cases Windows: it ignores COLORTERM and uses
    ``windows_console_features`` (or WINDOWS for a legacy console).  On CI and
    redirected streams that reports 16-color, which breaks truecolor and OSC 8
    link rendering.  Reading COLORTERM/TERM directly keeps macOS and Windows
    consistent with what the user asked for.
    """
    if os.environ.get("TERM", "").strip().lower() in ("dumb", "unknown"):
        return "standard"
    color_term = os.environ.get("COLORTERM", "").strip().lower()
    if color_term in ("truecolor", "24bit"):
        return "truecolor"
    term = os.environ.get("TERM", "").strip().lower()
    _name, _hyphen, colors = term.rpartition("-")
    if colors == "256color":
        return "256"
    return "standard"


def render_to_ansi(renderable: RenderableType, *, columns: int) -> str:
    """Render a Rich renderable to an ANSI string for prompt_toolkit integration."""
    from io import StringIO

    width = max(20, columns)
    buf = StringIO()
    temp = Console(
        file=buf,
        force_terminal=True,
        width=width,
        theme=NEUTRAL_MARKDOWN_THEME,
        highlight=False,
        legacy_windows=False,
        color_system=_ansi_color_system(),
    )
    temp.print(renderable, end="")
    result = buf.getvalue()
    return _OSC8_RE.sub(_wrap_osc8_as_zero_width, result)
