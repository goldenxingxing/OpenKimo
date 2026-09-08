"""Targeted edits for kimi-cli's ``config.toml``.

Why this exists: kimi-cli's env-merge in ``web/api/config.py:_build_global_config``
will *add* ``[models.X]`` / ``[providers.X]`` from ``LLM_PROVIDERS`` but never
update or remove existing entries. Without active intervention, providers the
user deletes in the Settings UI keep haunting the saved config (and surface
as duplicates in the model selector), and edits to an existing provider's
``base_url`` / ``api_key`` are silently ignored because the toml entry wins.

``prune`` is line-based to preserve comments and hand-edited formatting when
deleting whole sections. ``update_providers`` reuses ``tomlkit`` (bundled by
venvstacks) so an upsert can update individual keys in place without losing
surrounding comments or unrelated keys.

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
from typing import Any

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


def prune(path: Path, keep_names: set[str], *, allow_empty: bool = False) -> None:
    """Drop ``[models.X]`` / ``[providers.X]`` blocks where X is not in
    ``keep_names``, and clear ``default_model`` if it points outside the set.

    No-ops if the file doesn't exist, or if ``keep_names`` is empty and
    ``allow_empty`` is False — the guard exists so a caller that has not
    computed a real allowlist cannot wipe the file by accident.

    ``allow_empty=True`` is how a caller says the empty set is the answer: the
    user deleted every provider in Settings. Without it, that case skipped the
    sync entirely and ``_build_global_config`` kept serving the deleted
    providers in the model selector — the exact drift this module exists to
    prevent.
    """
    if not path.exists() or (not keep_names and not allow_empty):
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


def update_providers(path: Path, providers: list[dict[str, Any]]) -> None:
    """Upsert ``[providers.X]`` sections in ``config.toml`` from parsed
    ``LLM_PROVIDERS`` entries.

    For each provider dict (shape: at minimum ``name``; optionally ``type``,
    ``base_url``, ``api_key``), write or overwrite the env-managed keys on
    the matching ``[providers.<name>]`` table. Other keys in the table
    (e.g. hand-edited ``custom_headers``, ``reasoning_key``, ``oauth``) are
    left intact, as are surrounding comments and formatting.

    Empty ``base_url`` / ``api_key`` values are skipped rather than written,
    so a missing field in the env entry does not clobber a value already in
    the toml.

    No-ops if ``providers`` is empty (we don't want to materialise sections
    the caller hasn't actually computed yet) or the file doesn't exist
    (kimi-cli writes a fresh ``config.toml`` on first run; nothing to upsert
    into until then).
    """
    if not providers or not path.exists():
        return

    import tomlkit

    text = path.read_text(encoding="utf-8")
    try:
        doc = tomlkit.parse(text)
    except Exception:
        # Malformed TOML — bail rather than corrupt the file. The runtime
        # will surface its own validation error on next load.
        return

    providers_tbl = doc.get("providers")
    if providers_tbl is None:
        providers_tbl = tomlkit.table(is_super_table=True)
        doc["providers"] = providers_tbl

    changed = False
    for entry in providers:
        name = str(entry.get("name", "")).strip()
        if not name:
            continue

        sub = providers_tbl.get(name)
        if sub is None:
            sub = tomlkit.table()
            providers_tbl[name] = sub

        ptype = entry.get("type")
        if ptype:
            sub["type"] = str(ptype)
            changed = True

        base_url = entry.get("base_url")
        if base_url:
            sub["base_url"] = str(base_url)
            changed = True

        api_key = entry.get("api_key")
        if api_key:
            sub["api_key"] = str(api_key)
            changed = True

    if not changed:
        return

    new_text = tomlkit.dumps(doc)
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


def update_models(path: Path, models: list[dict[str, Any]]) -> None:
    """Upsert ``[models.X]`` sections in ``config.toml`` from parsed
    ``LLM_PROVIDERS`` entries.

    Mirror of ``update_providers``: kimi-cli's ``_build_global_config`` only
    seeds ``config.models[name]`` when ``name`` is absent, so edits to an
    existing model's ``max_context_size`` / ``capabilities`` via the Settings
    UI never reach the toml. We write the env-managed fields in place, leaving
    hand-edited keys intact. ``display_name`` is one of those keys until an env
    entry names it: the Settings UI does not write one, so a hand-edited
    display name survives, but a display_name spelled out in ``LLM_PROVIDERS``
    is an instruction and is honoured.

    Each entry uses the provider's ``name`` as the model key (matches kimi-cli
    line ``config.models[name] = LLMModel(provider=name, ...)``).

    Empty / missing scalar values are skipped rather than written so a blank
    field in the env entry does not clobber a value already in the toml. The
    ``capabilities`` key is overwritten whenever it is present in the entry
    (including as an empty list); a missing key leaves the toml value alone.

    No-ops on empty list, missing file, or unparseable toml.
    """
    if not models or not path.exists():
        return

    import tomlkit

    text = path.read_text(encoding="utf-8")
    try:
        doc = tomlkit.parse(text)
    except Exception:
        return

    models_tbl = doc.get("models")
    if models_tbl is None:
        models_tbl = tomlkit.table(is_super_table=True)
        doc["models"] = models_tbl

    changed = False
    for entry in models:
        name = str(entry.get("name", "")).strip()
        if not name:
            continue

        sub = models_tbl.get(name)
        if sub is None:
            sub = tomlkit.table()
            models_tbl[name] = sub

        provider = entry.get("provider") or entry.get("name")
        if provider:
            sub["provider"] = str(provider)
            changed = True

        model_id = entry.get("model")
        if model_id:
            sub["model"] = str(model_id)
            changed = True

        max_ctx = entry.get("max_context_size")
        if max_ctx:
            try:
                sub["max_context_size"] = int(max_ctx)
                changed = True
            except (TypeError, ValueError):
                pass

        if "capabilities" in entry:
            caps_raw = entry.get("capabilities")
            if isinstance(caps_raw, list):
                arr = tomlkit.array()
                for c in caps_raw:
                    if c:
                        arr.append(str(c))
                sub["capabilities"] = arr
                changed = True

        display_name = entry.get("display_name")
        if display_name:
            sub["display_name"] = str(display_name)
            changed = True

    if not changed:
        return

    new_text = tomlkit.dumps(doc)
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


def set_default_model(path: Path, default: str) -> None:
    """Set the top-level ``default_model`` key in ``config.toml``.

    The env var name is ``LLM_DEFAULT_PROVIDER`` but kimi-cli writes its value
    to the toml field ``default_model`` (see ``_build_global_config``: each
    LLM_PROVIDERS entry's ``name`` becomes both the provider key and the model
    key, so the env's "provider" is the same string as the toml's "model").
    Pass the env value directly.

    No-ops if ``default`` is empty, the file is missing, the toml is
    unparseable, or the existing value already matches.
    """
    default = (default or "").strip()
    if not default or not path.exists():
        return

    import tomlkit

    text = path.read_text(encoding="utf-8")
    try:
        doc = tomlkit.parse(text)
    except Exception:
        return

    if str(doc.get("default_model", "")) == default:
        return

    doc["default_model"] = default

    new_text = tomlkit.dumps(doc)
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


DEFAULT_COMPACTION_RATIO = 0.95


#: Where other agents keep their skills. Listing these in ``skip_skill_dirs``
#: is what the Settings switch does; they are named explicitly so the config
#: file says what is being skipped rather than hiding it behind a flag.
OTHER_AGENT_SKILL_DIRS: list[str] = [
    "~/.claude/skills",
    "~/.codex/skills",
    "~/.agents/skills",
    "~/.config/agents/skills",
]


def get_skipped_skill_dirs(path: Path) -> list[str]:
    """Read ``skip_skill_dirs`` from ``config.toml``."""
    if not path.exists():
        return []
    try:
        import tomlkit

        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        value = doc.get("skip_skill_dirs")
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    return [str(v) for v in value]  # type: ignore[union-attr]


def set_skipped_skill_dirs(path: Path, dirs: list[str]) -> None:
    """Set top-level ``skip_skill_dirs`` in ``config.toml``.

    Directories listed here are left out of skill auto-discovery. Every
    discovered skill costs a few hundred characters of context on every single
    request, so another agent's catalogue sitting in ``~/.claude/skills`` is
    paid for whether or not it is ever used.

    Writes an empty list rather than removing the key: an explicit ``[]`` says
    "nothing is skipped" where a missing key says nothing at all, and the
    difference matters when reading the file back to populate a checkbox.

    Creates the file if it is missing. On first run the Settings window opens
    before the server has ever written config.toml, and this setting has no
    .env counterpart, so returning early discarded the switch silently.
    """
    import tomlkit

    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        doc = tomlkit.parse(text)
    except Exception:
        return

    existing = doc.get("skip_skill_dirs")
    if isinstance(existing, list) and [str(v) for v in existing] == dirs:  # type: ignore[union-attr]
        return
    doc["skip_skill_dirs"] = dirs

    new_text = tomlkit.dumps(doc)
    if new_text == text:
        return

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
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


def get_compaction_ratio(path: Path) -> float:
    """Read the current ratio, falling back to kimi-cli's default."""
    if not path.exists():
        return DEFAULT_COMPACTION_RATIO
    try:
        import tomlkit

        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        section = doc.get("loop_control")
        if section is None:
            return DEFAULT_COMPACTION_RATIO
        value = float(section.get("compaction_trigger_ratio", DEFAULT_COMPACTION_RATIO))
    except Exception:
        return DEFAULT_COMPACTION_RATIO
    return value if 0.5 <= value <= 0.99 else DEFAULT_COMPACTION_RATIO


def set_compaction_ratio(path: Path, ratio: float) -> None:
    """Set ``[loop_control] compaction_trigger_ratio`` in ``config.toml``.

    This is the share of the context window that may fill before the agent
    auto-compacts its history. kimi-cli validates the range on load, so a value
    outside 0.5-0.99 is rejected here rather than written and blamed later.

    Creates the file if it is missing. On first run the Settings window opens
    before the server has ever started, so sessions/config.toml does not exist
    yet — and this setting has no .env counterpart, so returning early meant
    the slider was silently discarded and reverted on the next open.

    No-ops if the toml is unparseable or the value already matches.
    """
    # Range first. It used to sit after the existence check, so
    # set_compaction_ratio(missing_path, 5.0) returned quietly instead of
    # refusing — the "rejected rather than written" contract held only when
    # the file already existed.
    if not (0.5 <= ratio <= 0.99):
        raise ValueError(f"compaction_trigger_ratio must be within 0.5-0.99, got {ratio}")

    import tomlkit

    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        doc = tomlkit.parse(text)
    except Exception:
        return

    section = doc.get("loop_control")
    if section is None:
        section = tomlkit.table()
        doc["loop_control"] = section
    else:
        # A non-numeric existing value used to raise a bare ValueError from
        # float() — one without the "0.5-0.99" text, so settings_window's
        # `except Exception: log.exception(...)` swallowed it and the user's
        # slider change vanished with no message.
        try:
            current = float(section.get("compaction_trigger_ratio", -1))
        except (TypeError, ValueError):
            current = None
        if current == ratio:
            return

    section["compaction_trigger_ratio"] = ratio

    new_text = tomlkit.dumps(doc)
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


__all__ = [
    "prune",
    "update_providers",
    "update_models",
    "set_default_model",
    "set_compaction_ratio",
    "get_compaction_ratio",
    "DEFAULT_COMPACTION_RATIO",
]
