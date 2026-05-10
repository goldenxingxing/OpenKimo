"""Minimal `.env` reader/writer with whitelist enforcement.

We avoid python-dotenv to keep the app layer thin. The format we accept is
the strict subset used by `scripts/start.sh`: KEY=VALUE lines, optional
single/double quotes around VALUE, `#` introduces a comment, blank lines
preserved on write.

Settings UI only ever shows EDITABLE_KEYS; unknown lines are kept verbatim
on save so users' hand-edits aren't trampled.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

# Whitelisted keys exposed in the Settings window. Single source of truth.
EDITABLE_KEYS: tuple[str, ...] = (
    # LLM — new multi-provider config (YAML/JSON list)
    "LLM_PROVIDERS",
    "LLM_DEFAULT_PROVIDER",
    # LLM — legacy single-provider fallbacks (kept editable so the UI can clear
    # them when migrating to LLM_PROVIDERS)
    "LLM_PROVIDER",
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
    "KIMI_MODEL_NAME",
    "KIMI_MODEL_MAX_CONTEXT_SIZE",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "LLM_THINKING",
    "LLM_TEMPERATURE",
    # Web server
    "KIMI_WEB_PORT",
    "KIMI_WEB_SESSION_TOKEN",
    "KIMI_WEB_LAN_ONLY",
    # Paths
    "KIMI_DEFAULT_WORK_DIR",
    "KIMI_SESSION_DATA_DIR",
    "KIMI_OUTPUT_DIR",
    "CUSTOM_SKILLS_HOST_PATH",
    "HF_CACHE_HOST_PATH",
)

SECRET_KEYS: frozenset[str] = frozenset({
    "KIMI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "KIMI_WEB_SESSION_TOKEN",
    # LLM_PROVIDERS embeds API keys; treat the whole blob as secret.
    "LLM_PROVIDERS",
})

_LINE_RE = re.compile(r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<val>.*)$")


def _unquote(v: str) -> str:
    v = v.rstrip()
    if len(v) >= 2 and v[0] == v[-1]:
        if v[0] == "'":
            # Single-quoted: literal value (matches bash semantics).
            return v[1:-1]
        if v[0] == '"':
            # Double-quoted: unescape the same sequences that ``_quote`` emits.
            inner = v[1:-1]
            return inner.replace('\\"', '"').replace("\\\\", "\\")
    return v


def _quote(v: str) -> str:
    if v == "":
        return ""
    has_dq = '"' in v
    has_sq = "'" in v
    if not re.search(r"[\s#'\"]", v):
        return v
    # Prefer single quotes when the value carries embedded double quotes (JSON
    # blobs, e.g. LLM_PROVIDERS) and no single quotes — single-quoted bash
    # values need no escaping and round-trip unchanged through ``_unquote``.
    if has_dq and not has_sq:
        return "'" + v + "'"
    # Fall back to double quotes; escape backslash and double-quote so bash
    # ``source`` and our ``_unquote`` both reproduce the original value.
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read_env(path: Path) -> dict[str, str]:
    """Return all KEY=VALUE pairs from `path`, or {} if the file is missing."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _LINE_RE.match(raw)
        if not m:
            continue
        out[m["key"]] = _unquote(m["val"])
    return out


def read_editable(path: Path) -> dict[str, str]:
    """Same as read_env, but filtered to EDITABLE_KEYS."""
    return {k: v for k, v in read_env(path).items() if k in EDITABLE_KEYS}


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Atomically merge `updates` into `path`.

    Rules:
      - Only keys in EDITABLE_KEYS may be updated.
      - An empty string value REMOVES the line.
      - Lines we don't recognise (comments, unknown keys, blanks) pass
        through verbatim.
      - New keys are appended at the end with a clear separator.
    """
    invalid = [k for k in updates if k not in EDITABLE_KEYS]
    if invalid:
        raise ValueError(f"refusing to write non-whitelisted keys: {invalid}")

    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text().splitlines()

    seen: set[str] = set()
    new_lines: list[str] = []
    for raw in existing_lines:
        m = _LINE_RE.match(raw)
        if not m or m["key"] not in updates:
            new_lines.append(raw)
            continue
        key = m["key"]
        seen.add(key)
        new_val = updates[key]
        if new_val == "":
            # Drop the line entirely.
            continue
        new_lines.append(f"{key}={_quote(new_val)}")

    appended: list[str] = []
    for key, val in updates.items():
        if key in seen or val == "":
            continue
        appended.append(f"{key}={_quote(val)}")

    if appended:
        if new_lines and new_lines[-1].strip() != "":
            new_lines.append("")
        new_lines.append("# --- Added by Settings ---")
        new_lines.extend(appended)

    text = "\n".join(new_lines).rstrip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def mask_secret(value: str) -> str:
    """Render a sensitive value for display."""
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return value[:4] + "•" * 12 + value[-4:]


def load_into_environ(path: Path) -> None:
    """Inject `.env` values into os.environ (does not override existing)."""
    for k, v in read_env(path).items():
        os.environ.setdefault(k, v)
