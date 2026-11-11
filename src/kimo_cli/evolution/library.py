"""Evolution library: storage layout and access for self-evolution data.

Layout under ``get_share_dir()/evolution/``::

    evolution/
    ├── EXPERIENCE.md          # experience index: when a change worked, evidence pointers
    ├── proposals/<id>/        # editor-produced candidates awaiting verification
    ├── adopted/<id>/          # human-approved improvements (applied when the switch is on)
    └── verify_runs/<prop_id>/ # raw dual-run artifacts + evidence.json

A proposal/adopted directory holds:
    PROPOSAL.md                # human-readable rationale and target task types
    system_append.md           # candidate system prompt append (optional)
    skills/<name>/SKILL.md     # candidate skills (optional, multiple)
    meta.json                  # {id, created_at, editor_model, source_tasks, status, ...}
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from kimo_cli.config import Config
from kimo_cli.share import get_share_dir
from kimo_cli.utils.io import atomic_json_write
from kimo_cli.utils.logging import logger

EXPERIENCE_FILE_NAME = "EXPERIENCE.md"

# `generations.json` is read-modify-written by several entry points (adopt,
# rollback, advance, set_adopted_enabled, initial load). The CLI process and
# the web admin API run in different processes and can both touch the file
# at once — atomic_json_write protects against torn writes but not against
# concurrent RMW losing an update. Guard every RMW with an exclusive
# fcntl lock on a sidecar file so a later update is never lost to a race
# with an earlier one. Fallback to a threading.RLock on Windows (no fcntl).
if sys.platform.startswith("win"):
    import threading

    _generations_lock_inner: threading.RLock = threading.RLock()

    @contextlib.contextmanager
    def _generations_lock() -> Iterator[None]:
        with _generations_lock_inner:
            yield
else:
    import fcntl

    @contextlib.contextmanager
    def _generations_lock() -> Iterator[None]:
        lock_path = generations_file().with_suffix(generations_file().suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

# `generations.json` is read-modify-written by several entry points (adopt,
# rollback, advance, set_adopted_enabled, initial load). The CLI process and
# the web admin API run in different processes and can both touch the file
# at once — an atomic_json_write protects against torn writes but not
# against concurrent RMW losing an update. Guard every RMW with an
# exclusive fcntl lock on a sidecar file so a later update is never lost
# to a race with an earlier one.


class EvolutionGateError(RuntimeError):
    """Raised when a token-consuming evolution operation is attempted while the
    global self-evolution switch (``default_self_evolve``) is off."""


def evolution_dir() -> Path:
    return get_share_dir() / "evolution"


def proposals_dir() -> Path:
    return evolution_dir() / "proposals"


def adopted_dir() -> Path:
    return evolution_dir() / "adopted"


def verify_runs_dir() -> Path:
    return evolution_dir() / "verify_runs"


def experience_file() -> Path:
    return evolution_dir() / EXPERIENCE_FILE_NAME


def evolution_enabled(config: Config) -> bool:
    """The single authority for the self-evolution switch."""
    return config.default_self_evolve


def require_enabled(config: Config) -> None:
    """Gate for token-consuming evolution operations (propose/verify/adopt)."""
    if not evolution_enabled(config):
        raise EvolutionGateError(
            "Self-evolution is off (config default_self_evolve = false). "
            "Flip the Self-evolve toggle in the web UI (or set default_self_evolve "
            "= true in ~/.kimo/config.toml) to authorise token-consuming "
            "evolution operations. Evidence collection is always on and needs "
            "no switch."
        )


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Records aggregation (evidence layer input)
# ---------------------------------------------------------------------------


@dataclass
class TurnEvidence:
    """One parsed line of a session's evolution/records.jsonl."""

    session_dir: Path
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    steps: int = 0
    outcome: str = "unknown"
    task: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _parse_records_file(records_file: Path) -> list[TurnEvidence]:
    """Parse one ``evolution/records.jsonl`` into turn evidence rows.

    Tolerates missing fields (records written before the ``task`` field
    existed) and corrupted lines.
    """
    session_dir = records_file.parent.parent
    try:
        lines = records_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        logger.warning("Unreadable evolution records: {path}", path=records_file)
        return []
    out: list[TurnEvidence] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append(
            TurnEvidence(
                session_dir=session_dir,
                input_tokens=int(d.get("input_tokens") or 0),
                output_tokens=int(d.get("output_tokens") or 0),
                reasoning_tokens=int(d.get("reasoning_tokens") or 0),
                steps=int(d.get("steps") or 0),
                outcome=str(d.get("outcome") or "unknown"),
                task=d.get("task"),
            )
        )
    return out


def iter_session_records(sessions_root: Path) -> list[TurnEvidence]:
    """Parse every evolution/records.jsonl under the sessions root."""
    out: list[TurnEvidence] = []
    if not sessions_root.is_dir():
        return out
    for records_file in sorted(sessions_root.rglob("records.jsonl")):
        if records_file.parent.name != "evolution":
            continue
        out.extend(_parse_records_file(records_file))
    return out


def _local_work_dir_session_roots() -> list[Path]:
    """``session-data`` roots of every local work dir, from the metadata ledger.

    Local work-dir sessions live under ``<work_dir>/session-data`` (see
    ``metadata.WorkDirMeta.sessions_dir``); the legacy location under the
    share dir is already covered by ``iter_session_records(get_share_dir() /
    "sessions")`` via its recursive glob.  Non-local (KAOS remote) work dirs
    are excluded here — their session roots live under the share dir and are
    already covered.
    """
    roots: list[Path] = []
    try:
        from kimo_cli.metadata import load_metadata as _load_metadata

        metadata = _load_metadata()
    except Exception:
        return roots
    for wd in getattr(metadata, "work_dirs", []):
        # Match metadata.WorkDirMeta.sessions_dir: only local work dirs get a
        # per-workdir session-data root.
        if getattr(wd, "kaos", None) != "local":
            continue
        try:
            root = wd.sessions_dir
        except Exception:
            continue
        if root not in roots:
            roots.append(root)
    return roots


def session_record_roots() -> list[Path]:
    """All session evidence roots: share App sessions + every local work dir's
    ``session-data``.  ``iter_session_records`` alone (single root) misses
    workspace sessions entirely, which is why the admin panel showed no
    evidence despite real records under ``<work_dir>/session-data``."""
    roots: list[Path] = [get_share_dir() / "sessions"]
    for root in _local_work_dir_session_roots():
        if root not in roots:
            roots.append(root)
    return roots


def iter_all_session_records() -> list[TurnEvidence]:
    """Aggregate turn evidence across every session root (see
    ``session_record_roots``).  Records files are disjoint by construction, but
    dedupe by ``records.jsonl`` path defensively so a file reachable through
    two roots is never parsed twice.  Every line inside a file stays a separate
    row — the dedupe must NOT collapse a session's multiple turns."""
    out: list[TurnEvidence] = []
    seen_files: set[Path] = set()
    for root in session_record_roots():
        if not root.is_dir():
            continue
        for records_file in sorted(root.rglob("records.jsonl")):
            if records_file.parent.name != "evolution":
                continue
            if records_file in seen_files:
                continue
            seen_files.add(records_file)
            out.extend(_parse_records_file(records_file))
    return out


def task_groups(
    records: list[TurnEvidence],
    pattern: str | None = None,
) -> list[tuple[str, list[TurnEvidence]]]:
    """Group replayable records by task text, worst-first.

    Excludes records without a task (slash turns, media-only prompts).
    Sort key: more failures first, then higher average token cost.
    """
    groups: dict[str, list[TurnEvidence]] = {}
    for r in records:
        if not r.task:
            continue
        if pattern and pattern not in r.task:
            continue
        groups.setdefault(r.task, []).append(r)

    def key(item: tuple[str, list[TurnEvidence]]) -> tuple[int, float]:
        turns = item[1]
        fails = sum(1 for t in turns if t.outcome != "completed")
        avg_tokens = sum(t.total_tokens for t in turns) / len(turns)
        return (-fails, -avg_tokens)

    return sorted(groups.items(), key=key)


# ---------------------------------------------------------------------------
# Proposal / adopted IO
# ---------------------------------------------------------------------------


@dataclass
class Entry:
    """A proposal or adopted improvement directory."""

    id: str
    path: Path
    meta: dict = field(default_factory=dict)

    @property
    def system_append_path(self) -> Path:
        return self.path / "system_append.md"

    @property
    def skills_path(self) -> Path:
        return self.path / "skills"

    @property
    def proposal_md_path(self) -> Path:
        return self.path / "PROPOSAL.md"


def _load_entry(path: Path) -> Entry:
    """Load an entry from disk. Always returns an Entry (meta may be empty
    if meta.json is missing or corrupt); never returns None."""
    meta_file = path / "meta.json"
    meta: dict = {}
    if meta_file.exists():
        try:
            loaded = json.loads(meta_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
            else:
                logger.warning("Bad meta.json in {path}, continuing without it", path=path)
        except (json.JSONDecodeError, OSError):
            logger.warning("Bad meta.json in {path}, continuing without it", path=path)
    entry_id = str(meta.get("id") or path.name)
    return Entry(id=entry_id, path=path, meta=meta)


def list_entries(root: Path) -> list[Entry]:
    if not root.is_dir():
        return []
    return [_load_entry(p) for p in sorted(root.iterdir()) if p.is_dir()]


def list_proposals() -> list[Entry]:
    return list_entries(proposals_dir())


def list_adopted() -> list[Entry]:
    return list_entries(adopted_dir())


def save_meta(entry_path: Path, meta: dict) -> None:
    atomic_json_write(meta, entry_path / "meta.json")


def read_system_append(entry: Entry) -> str | None:
    """The append text if present and non-trivial, else None."""
    p = entry.system_append_path
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _adopted_entries_by_ids(ids: list[str]) -> list[Entry]:
    by_id = {e.id: e for e in list_entries(adopted_dir())}
    missing = [i for i in ids if i not in by_id]
    if missing:
        # An adopted id whose directory is gone (deleted by hand, or a meta id
        # that no longer matches the directory name) silently vanished from the
        # applied set; say so instead.
        logger.warning("Adopted entries not found on disk: {ids}", ids=missing)
    return [by_id[i] for i in ids if i in by_id]


def adopted_system_appends() -> list[tuple[str, str]]:
    """(adopted_id, append_text) pairs in the CURRENT generation, for the
    application channel."""
    out: list[tuple[str, str]] = []
    for entry in _adopted_entries_by_ids(current_adopted_ids()):
        text = read_system_append(entry)
        if text is not None:
            out.append((entry.id, text))
    return out


def adopted_skill_roots() -> list[Path]:
    """Parent directories holding skills of the CURRENT generation, for
    extra_skill_dirs."""
    return [
        e.skills_path
        for e in _adopted_entries_by_ids(current_adopted_ids())
        if e.skills_path.is_dir()
    ]


# ---------------------------------------------------------------------------
# Generations (version management) & audit
# ---------------------------------------------------------------------------

GENERATIONS_FILE_NAME = "generations.json"
ACTIONS_FILE_NAME = "actions.jsonl"


def generations_file() -> Path:
    return evolution_dir() / GENERATIONS_FILE_NAME


def _is_valid_generation_state(state: object) -> bool:
    if not isinstance(state, dict):
        return False
    generations = state.get("generations")
    if not isinstance(generations, list):
        return False
    current = state.get("current", len(generations))
    disabled = state.get("disabled_ids", [])
    return (
        isinstance(current, int)
        and not isinstance(current, bool)
        and 0 <= current <= len(generations)
        and isinstance(disabled, list)
        and all(isinstance(adopted_id, str) for adopted_id in disabled)
        and all(
            isinstance(generation, dict)
            and isinstance(generation.get("adopted_ids"), list)
            and all(isinstance(adopted_id, str) for adopted_id in generation["adopted_ids"])
            for generation in generations
        )
    )


def load_generations() -> dict:
    """Generation snapshots + current pointer + disabled overlay.

    Migration: a library written before generations existed gets gen 1 = all
    adopted entries, so nothing that used to apply stops applying.
    """
    path = generations_file()
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Bad generations.json, rebuilding from adopted entries")
        else:
            if _is_valid_generation_state(state):
                state.setdefault("current", len(state["generations"]))
                state.setdefault("disabled_ids", [])
                return state
            logger.warning("Bad generations.json, rebuilding from adopted entries")
    adopted_ids = [e.id for e in list_entries(adopted_dir())]
    state = {
        "generations": [
            {"n": 1, "adopted_ids": adopted_ids, "created_at": time.time(), "note": "migrated"}
        ]
        if adopted_ids
        else [],
        "current": 1 if adopted_ids else 0,
        "disabled_ids": [],
    }
    if adopted_ids:
        save_generations(state)
    return state


def save_generations(state: dict) -> None:
    atomic_json_write(state, generations_file())


def current_adopted_ids() -> list[str]:
    """Adopted entries in effect: current generation minus disabled overlay."""
    state = load_generations()
    current = int(state.get("current") or 0)
    if current < 1 or current > len(state["generations"]):
        return []
    ids = list(state["generations"][current - 1].get("adopted_ids") or [])
    disabled = set(state.get("disabled_ids") or [])
    return [i for i in ids if i not in disabled]


def current_generation_n() -> int:
    return int(load_generations().get("current") or 0)


def record_adoption(adopted_id: str) -> int:
    """Append a new generation adding one entry to the current set."""
    with _generations_lock():
        state = load_generations()
        current = int(state.get("current") or 0)
        base = (
            list(state["generations"][current - 1].get("adopted_ids") or []) if current >= 1 else []
        )
        if adopted_id in base:
            return current
        # Hold the lock across the whole read-modify-write so a concurrent RMW
        # cannot slip in between our read and the write below.
        n = len(state["generations"]) + 1
        state["generations"].append(
            {
                "n": n,
                "adopted_ids": base + [adopted_id],
                "created_at": time.time(),
                "note": f"adopt {adopted_id}",
            }
        )
        state["current"] = n
        save_generations(state)
        return n


def rollback_generation() -> int:
    """Move the pointer one generation back. Returns the new current n."""
    with _generations_lock():
        state = load_generations()
        current = int(state.get("current") or 0)
        if current <= 1:
            raise ValueError("Already at the first generation; cannot roll back further.")
        state["current"] = current - 1
        save_generations(state)
        return current - 1


def advance_generation() -> int:
    """Move the pointer one generation forward (re-apply a rolled-back change)."""
    with _generations_lock():
        state = load_generations()
        current = int(state.get("current") or 0)
        if current >= len(state["generations"]):
            raise ValueError("Already at the newest generation; nothing to advance to.")
        state["current"] = current + 1
        save_generations(state)
        return current + 1


def set_adopted_enabled(adopted_id: str, enabled: bool) -> None:
    """Toggle a single entry without touching generation history (diagnostic
    overlay). Disabled entries stay out of the applied set until re-enabled."""
    with _generations_lock():
        state = load_generations()
        disabled = list(state.get("disabled_ids") or [])
        if enabled and adopted_id in disabled:
            disabled.remove(adopted_id)
        elif not enabled and adopted_id not in disabled:
            disabled.append(adopted_id)
        state["disabled_ids"] = disabled
        save_generations(state)


def disabled_ids() -> list[str]:
    return list(load_generations().get("disabled_ids") or [])


def log_action(action: str, actor: str, detail: object = None) -> None:
    """Append one line to the audit log; never raises."""
    try:
        path = evolution_dir() / ACTIONS_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), "actor": actor, "action": action, "detail": detail}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        logger.warning("Failed to write evolution audit log", exc_info=True)


def read_actions(limit: int = 50) -> list[dict]:
    path = evolution_dir() / ACTIONS_FILE_NAME
    if not path.exists():
        return []
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(out))


# ---------------------------------------------------------------------------
# Application channel (read by soul/agent.py)
# ---------------------------------------------------------------------------

PATCH_ENV_VAR = "KIMO_EVOLUTION_PATCH"
SKILLS_ENV_VAR = "KIMO_EVOLUTION_SKILLS_DIR"


def applied_system_append(config: Config) -> str | None:
    """System prompt append for agent creation.

    Precedence: the verify injection env var wins (unconditional, used by
    ``kimo evolve verify`` dual-runs); otherwise adopted improvements are
    mounted only when the global switch is on. Returns None when nothing
    applies — the common case stays zero-cost.
    """
    import os

    env_path = os.environ.get(PATCH_ENV_VAR)
    if env_path:
        try:
            text = Path(env_path).read_text(encoding="utf-8").strip()
            return text or None
        except OSError:
            logger.warning("Ignoring unreadable {var}={path}", var=PATCH_ENV_VAR, path=env_path)
    if not evolution_enabled(config):
        return None
    parts = [f"\n\n<!-- adopted:{pid} -->\n{text}" for pid, text in adopted_system_appends()]
    return "".join(parts) or None


def applied_skill_dirs(config: Config) -> list[Path]:
    """Skill roots to add to extra_skill_dirs for session creation."""
    import os

    env_dir = os.environ.get(SKILLS_ENV_VAR)
    if env_dir:
        p = Path(env_dir)
        return [p] if p.is_dir() else []
    if not evolution_enabled(config):
        return []
    return adopted_skill_roots()


# ---------------------------------------------------------------------------
# Adopt & experience index
# ---------------------------------------------------------------------------


def _shorten(text: str, width: int) -> str:
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    return text[: width - 3].rstrip() + "..."


def adopt_proposal(proposal_id: str, evidence_path: Path | None, actor: str = "cli") -> Entry:
    """Move a verified proposal into adopted/ and index it in EXPERIENCE.md.

    Transactional across the rename, metadata update, generations ledger,
    and experience append: if any step after the rename fails, the entry
    is moved back to proposals/ and the error re-raised, so a crash never
    leaves a half-adopted directory with no matching metadata.
    """
    src = proposals_dir() / proposal_id
    if not src.is_dir():
        raise FileNotFoundError(f"Proposal not found: {src}")
    dst = adopted_dir() / proposal_id
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise FileExistsError(f"Adopted entry already exists: {dst}")

    # Path traversal guard: reject anything that escapes adopted_dir().
    # The library relies on this because proposal_id is user-supplied via
    # the admin API; the editor's own output is regex-constrained, but
    # defense-in-depth keeps a future loosening from silently enabling
    # file move outside the intended root.
    _assert_under_root(proposal_id, dst, adopted_dir())

    src.rename(dst)
    try:
        entry = _load_entry(dst)
        meta = dict(entry.meta)
        meta["status"] = "adopted"
        meta["adopted_at"] = time.time()
        if evidence_path is not None:
            meta["evidence"] = str(evidence_path)
        save_meta(dst, meta)

        fresh = _load_entry(dst)
        entry = fresh
        record_adoption(proposal_id)

        proposal_md = entry.proposal_md_path
        rationale = (
            proposal_md.read_text(encoding="utf-8").strip()
            if proposal_md.exists()
            else "(no PROPOSAL.md)"
        )
        append_experience(
            f"## adopted:{proposal_id} ({time.strftime('%Y-%m-%d')})\n\n"
            f"- Evidence: {evidence_path}\n"
            f"- Rationale (excerpt): {_shorten(rationale, 500)}\n"
        )
        log_action("adopt", actor, {"proposal_id": proposal_id, "evidence": str(evidence_path)})
        return entry
    except Exception:
        # Roll back the rename so the proposal is back where it started.
        with contextlib.suppress(OSError):
            dst.rename(src)
        raise


def _assert_under_root(name: str, resolved: Path, root: Path) -> None:
    """Raise ValueError if `resolved` (which contains `name`) is outside `root`.

    Defense-in-depth against path traversal: `name` may contain `../` or
    other traversal sequences; the caller's regex usually blocks it, but a
    loosened regex or a hand-written proposal_id would otherwise let a
    proposal escape the intended root.
    """
    try:
        if not resolved.resolve().is_relative_to(root.resolve()):
            raise ValueError(
                f"Refusing to write {name!r} outside {root}: resolved to {resolved.resolve()}"
            )
    except ValueError:
        raise
    except OSError as e:
        raise ValueError(f"Cannot resolve {name!r}: {e}") from e


def append_experience(text: str) -> None:
    path = experience_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n\n")
