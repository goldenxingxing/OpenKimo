"""Targeted line-based pruning for kimi-cli's ``config.toml``.

Why this exists: kimi-cli's env-merge in ``web/api/config.py:_build_global_config``
will *add* ``[models.X]`` / ``[providers.X]`` from ``LLM_PROVIDERS`` but never
update or remove existing entries. Without active pruning, providers the user
deletes in the Settings UI keep haunting the saved config (and surface as
duplicates in the model selector). A line-based prune preserves comments and
the user's hand-edited formatting; a tomli_w round-trip would not (and we
don't bundle a TOML writer anyway).

Validator constraint (``kimi_cli.config.Config.validate_model``): if we drop
``[models.X]`` while leaving ``default_model = "X"`` in place, ``load_config``
will raise. So we also clear ``default_model`` when the value points at a
pruned entry — env's ``LLM_DEFAULT_PROVIDER`` re-seeds it on the next merge.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_SECTION_RE = re.compile(r"^\s*\[(?P<inner>[^\]]+)\]\s*(?:#.*)?$")
_DEFAULT_MODEL_RE = re.compile(
    r'^\s*default_model\s*=\s*"(?P<val>[^"]*)"\s*(?:#.*)?$'
)


def _section_first_subkey(top: str, inner: str) -> str | None:
    """Return the first dotted child of ``[<top>.<child>...]``, else None.

    Handles both bare (``[models.kimi]``) and quoted (``[models."kimi-k2.6"]``)
    forms. Returns None if ``inner`` doesn't begin with ``<top>.``.
    """
    prefix = f"{top}."
    if not inner.startswith(prefix):
        return None
    rest = inner[len(prefix):]
    if not rest:
        return None
    if rest[0] in ('"', "'"):
        quote = rest[0]
        end = rest.find(quote, 1)
        if end < 0:
            return None
        return rest[1:end]
    return rest.split(".", 1)[0]


def prune(path: Path, keep_names: set[str]) -> None:
    """Drop ``[models.X]`` / ``[providers.X]`` blocks where X is not in
    ``keep_names``, and clear ``default_model`` if it points outside the set.

    No-ops if the file doesn't exist or ``keep_names`` is empty (we don't want
    to wipe everything if the caller hasn't computed a real allowlist yet).
    """
    if not path.exists() or not keep_names:
        return

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    drop_section = False
    in_top_level = True

    for line in lines:
        m = _SECTION_RE.match(line)
        if m:
            in_top_level = False
            inner = m.group("inner").strip()
            child = (
                _section_first_subkey("models", inner)
                or _section_first_subkey("providers", inner)
            )
            if child is not None and child not in keep_names:
                drop_section = True
                continue
            drop_section = False
            out.append(line)
            continue

        if drop_section:
            continue

        if in_top_level:
            dm = _DEFAULT_MODEL_RE.match(line)
            if dm:
                val = dm.group("val")
                if val and val not in keep_names:
                    out.append('default_model = ""\n')
                    continue

        out.append(line)

    new_text = "".join(out)
    if new_text == text:
        return

    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


__all__ = ["prune"]
