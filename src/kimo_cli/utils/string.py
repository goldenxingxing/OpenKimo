from __future__ import annotations

import random
import re
import string

from carryover import fold_text as fold_text

_NEWLINE_RE = re.compile(r"[\r\n]+")


#: Re-exported from :mod:`carryover.text`, not reimplemented here.
#:
#: The Wiki's intent hashing is an authorization boundary and memory
#: deduplication decides whether two statements are the same. Both fold the
#: same three axes, and while there were two copies of that, one of them lost
#: two axes and nothing noticed. There is now one, in the package that also
#: uses it.
def shorten(text: str, *, width: int, placeholder: str = "…") -> str:
    """Shorten text to at most *width* characters.

    Normalises whitespace, then truncates — preferring a word boundary
    when one exists near the cut point, but falling back to a hard cut
    so that CJK text without spaces won't collapse to just the placeholder.
    """
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    cut = width - len(placeholder)
    if cut <= 0:
        return text[:width]
    space = text.rfind(" ", 0, cut + 1)
    if space > 0:
        cut = space
    return text[:cut].rstrip() + placeholder


_PASTED_TEXT_RE = re.compile(r'<pasted_text index="\d+">.*?</pasted_text>', re.DOTALL)
_UPLOADED_FILES_RE = re.compile(r"<uploaded_files>.*?</uploaded_files>", re.DOTALL)
_FILE_BLOCK_RE = re.compile(r"<(document|image|video)\b([^>]*)>.*?</\1>", re.DOTALL)
_FILE_TAG_RE = re.compile(r"<file\b([^>]*)/>")
_PATH_ATTR_RE = re.compile(r'\bpath="([^"]*)"')


def _file_label(attrs: str) -> str:
    match = _PATH_ATTR_RE.search(attrs)
    name = match.group(1).replace("\\", "/").rsplit("/", 1)[-1] if match else ""
    return f"⟦{name}⟧" if name else ""


def fold_message_markup(text: str) -> str:
    """A web message as the person wrote it, for a title or a summary.

    The web UI sends a long paste as ``<pasted_text>`` and each uploaded file
    as a tagged block holding its full path and contents. Titled from that
    text, a session was named ``…<pasted_text index="1">order_id,date,…`` or
    ``<uploaded_files> 1. /Users/…``. A paste becomes ``⟦…⟧`` and a file its
    name, where the person put them; the list of files is dropped.
    """
    text = _UPLOADED_FILES_RE.sub(" ", text)
    text = _PASTED_TEXT_RE.sub("⟦…⟧", text)
    text = _FILE_BLOCK_RE.sub(lambda m: _file_label(m.group(2)), text)
    return _FILE_TAG_RE.sub(lambda m: _file_label(m.group(1)), text)


def shorten_middle(text: str, width: int, remove_newline: bool = True) -> str:
    """Shorten the text to at most ``width`` characters, inserting an ellipsis.

    The result never exceeds ``width``. The previous version returned
    ``2 * (width // 2) + 3`` characters, and degenerated for ``width <= 1``:
    ``text[-0:]`` is the whole string, so a one-character budget produced the
    entire text with an ellipsis in front of it.
    """
    if len(text) <= width:
        return text
    if remove_newline:
        text = _NEWLINE_RE.sub(" ", text)
        if len(text) <= width:
            return text
    if width <= 3:
        # No room for content next to the ellipsis.
        return text[:width]
    # Keep the head at ``width // 2`` (the length callers used to see) and give
    # the remainder to the tail, so the result still fits into ``width``.
    head = width // 2
    tail = width - 3 - head
    if tail < 0:
        head = width - 3
        tail = 0
    return text[:head] + "..." + (text[-tail:] if tail else "")


def random_string(length: int = 8) -> str:
    """Generate a random string of fixed length."""
    letters = string.ascii_lowercase
    return "".join(random.choice(letters) for _ in range(length))
