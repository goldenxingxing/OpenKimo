"""Session fork utilities.

Provides turn enumeration, wire/context truncation, and session forking
for both the Web API and CLI slash commands (/undo, /fork).
"""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from kimo_cli.session_state import load_session_state, save_session_state

CHECKPOINT_USER_PATTERN = re.compile(r"^<system>CHECKPOINT \d+</system>$")

# The first TextPart of a context-compaction summary message. Kept in sync with
# kimo_cli.soul.compaction.COMPACTION_OUTPUT_PREFIX (deliberately not imported:
# compaction pulls in the LLM stack, and session_fork is a lightweight module
# used by web/CLI paths that must not pay that).
_COMPACTION_SUMMARY_MARKER = (
    "<system>Previous context has been compacted. Here is the compaction output:</system>"
)

# Blocks that record the *source session's in-flight state*, not conversation
# history: a forked session must not inherit these as fresh todo items (they
# describe what the source was about to do — e.g. "report the 616.5s timeout").
_COMPACTION_SNAPSHOT_TAGS = (
    "current_focus",
    "completed_tasks",
    "active_issues",
    "important_context",
    "code_state",
)

_COMPACTION_SNAPSHOT_TAGS_RE = "|".join(_COMPACTION_SNAPSHOT_TAGS)
_COMPACTION_SNAPSHOT_RE = re.compile(
    rf"<(?:{_COMPACTION_SNAPSHOT_TAGS_RE})>.*?</(?:{_COMPACTION_SNAPSHOT_TAGS_RE})>",
    re.DOTALL,
)


def _strip_compaction_snapshot_text(text: str) -> str:
    """Remove the in-flight-state blocks from one compaction-summary text part.

    Keeps the ``<system>`` marker, the conversation digest, and the objective
    ``files_read``/``files_modified``/``latest_user_request`` blocks; drops only
    the blocks that describe what the *source* session had not finished.
    """
    return _COMPACTION_SNAPSHOT_RE.sub("", text)


def _strip_compaction_snapshot_line(line: str) -> str:
    """Strip compaction-snapshot blocks from one context.jsonl line, if it is a
    compaction summary user message; otherwise return it unchanged."""
    try:
        record: dict[str, Any] = json.loads(line)
    except json.JSONDecodeError:
        return line
    if record.get("role") != "user":
        return line
    content = record.get("content")
    if not isinstance(content, list) or not content:
        return line
    first = content[0]
    if not (
        isinstance(first, dict)
        and first.get("type") == "text"
        and first.get("text") == _COMPACTION_SUMMARY_MARKER
    ):
        return line
    kept: list[Any] = []
    for part in content:
        if not (isinstance(part, dict) and part.get("type") == "text"):
            kept.append(part)
            continue
        stripped = _strip_compaction_snapshot_text(part.get("text", ""))
        if stripped.strip():
            part = dict(part)
            part["text"] = stripped
            kept.append(part)
    record["content"] = kept
    return json.dumps(record, ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class TurnInfo:
    """Summary of a single turn for display in the selector."""

    index: int
    """0-based turn index."""
    user_text: str
    """First-line text of the user message."""


# ---------------------------------------------------------------------------
# Turn enumeration
# ---------------------------------------------------------------------------


def enumerate_turns(wire_path: Path) -> list[TurnInfo]:
    """Scan wire.jsonl and return a list of all turns with user message text.

    Each turn is identified by a ``TurnBegin`` record, whose ``user_input``
    field provides the user message text.
    """
    if not wire_path.exists():
        return []

    turns: list[TurnInfo] = []
    current_turn = -1

    with open(wire_path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue

            try:
                record: dict[str, Any] = json.loads(stripped)
            except json.JSONDecodeError:
                continue

            if record.get("type") == "metadata":
                continue

            message: dict[str, Any] = record.get("message", {})
            msg_type: str | None = message.get("type")

            if msg_type == "TurnBegin":
                current_turn += 1
                user_input = message.get("payload", {}).get("user_input", "")
                text = _extract_user_text(user_input)
                turns.append(TurnInfo(index=current_turn, user_text=text))

    return turns


def _extract_user_text(user_input: str | list[Any]) -> str:
    """Extract plain text from a TurnBegin user_input (string or content parts)."""
    if isinstance(user_input, str):
        return user_input

    parts: list[str] = []
    for part in user_input:
        if isinstance(part, dict):
            part_dict = cast(dict[str, Any], part)
            text = part_dict.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(part, str):
            parts.append(part)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Wire / context truncation (extracted from web/api/sessions.py)
# ---------------------------------------------------------------------------


def truncate_wire_at_turn(wire_path: Path, turn_index: int) -> list[str]:
    """Read wire.jsonl and return all lines up to and including the given turn.

    Args:
        wire_path: Path to the wire.jsonl file
        turn_index: 0-based turn index. Returns turns 0..turn_index inclusive.

    Returns:
        List of raw JSON lines (including the metadata header)

    Raises:
        ValueError: If turn_index is out of range
    """
    if not wire_path.exists():
        raise ValueError("wire.jsonl not found")

    lines: list[str] = []
    current_turn = -1  # Will become 0 on first TurnBegin

    with open(wire_path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue

            try:
                record: dict[str, Any] = json.loads(stripped)
            except json.JSONDecodeError:
                continue

            # Always keep metadata header
            if record.get("type") == "metadata":
                lines.append(stripped)
                continue

            message: dict[str, Any] = record.get("message", {})
            msg_type: str | None = message.get("type")

            if msg_type == "TurnBegin":
                current_turn += 1
                if current_turn > turn_index:
                    break

            if current_turn <= turn_index:
                lines.append(stripped)

            # Stop after the TurnEnd of the target turn
            if msg_type == "TurnEnd" and current_turn == turn_index:
                break

    if current_turn < turn_index:
        raise ValueError(f"turn_index {turn_index} out of range (max turn: {current_turn})")

    return lines


def _is_checkpoint_user_message(record: dict[str, Any]) -> bool:
    """Whether a context line is the synthetic user checkpoint marker."""
    if record.get("role") != "user":
        return False

    content = record.get("content")
    if isinstance(content, str):
        return CHECKPOINT_USER_PATTERN.fullmatch(content.strip()) is not None

    parts = cast(list[Any], content) if isinstance(content, list) else []
    if len(parts) == 1 and isinstance(parts[0], dict):
        first_part = cast(dict[str, Any], parts[0])
        text = first_part.get("text")
        if isinstance(text, str):
            return CHECKPOINT_USER_PATTERN.fullmatch(text.strip()) is not None

    return False


def _wire_turn_inputs(wire_path: Path) -> list[str]:
    """Return TurnBegin inputs in wire order, including slash-only turns."""
    inputs: list[str] = []
    for raw_line in _read_all_lines(wire_path):
        try:
            line = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        message = line.get("message", {})
        if isinstance(message, dict) and message.get("type") == "TurnBegin":
            inputs.append(_extract_user_text(message.get("payload", {}).get("user_input", "")))
    return inputs


def truncate_context_at_turn(
    context_path: Path, turn_index: int, *, wire_path: Path | None = None
) -> list[str]:
    """Truncate context at the boundary derived from its wire turn.

    Context also contains synthetic user messages (compaction summaries,
    checkpoints and injected state), while slash-only wire turns may have no
    user message at all. When the wire is available, locate the next real
    turn's original input instead of counting context user records. The old
    counting fallback remains for standalone callers and legacy partial files.
    """
    if not context_path.exists():
        return []

    lines = _read_all_lines(context_path)
    if wire_path is not None and wire_path.exists():
        turn_inputs = _wire_turn_inputs(wire_path)
        if turn_index < 0 or turn_index >= len(turn_inputs):
            raise ValueError(
                f"turn_index {turn_index} out of range (have {len(turn_inputs)} turns)"
            )
        real_inputs = [
            text for text in turn_inputs if text.strip() and not text.lstrip().startswith("/")
        ]
        selected_inputs = [
            text
            for text in turn_inputs[: turn_index + 1]
            if text.strip() and not text.lstrip().startswith("/")
        ]
        next_inputs = real_inputs[len(selected_inputs) :]
        if not next_inputs:
            return lines

        def _matches_turn(line: str, user_input: str) -> bool:
            try:
                record: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                return False
            return (
                record.get("role") == "user"
                and not _is_checkpoint_user_message(record)
                and _extract_user_text(record.get("content", "")) == user_input
            )

        # Advance past selected real turns in order so repeated user text cannot
        # anchor the cut to an earlier occurrence. Synthetic context-only user
        # records are simply skipped by this alignment.
        search_start = 0
        for selected_input in selected_inputs:
            for index in range(search_start, len(lines)):
                if _matches_turn(lines[index], selected_input):
                    search_start = index + 1
                    break

        next_user_input = next_inputs[0]
        for index in range(search_start, len(lines)):
            if _matches_turn(lines[index], next_user_input):
                return lines[:index]
        return lines

    result = []
    current_turn = -1
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("role") == "user" and not _is_checkpoint_user_message(record):
            current_turn += 1
            if current_turn > turn_index:
                break
        if current_turn <= turn_index:
            result.append(line)
    return result


# ---------------------------------------------------------------------------
# Full fork operation
# ---------------------------------------------------------------------------


async def fork_session(
    source_session_dir: Path,
    work_dir: Any,  # KaosPath
    turn_index: int | None = None,
    title_prefix: str = "Fork",
    source_title: str | None = None,
) -> str:
    """Fork a session, creating a new session with history up to the given turn.

    Args:
        source_session_dir: Path to the source session directory.
        work_dir: The KaosPath work directory.
        turn_index: 0-based turn index (inclusive). If None, copy all turns.
        title_prefix: Prefix for the forked session title.
        source_title: Title of the source session (used for the fork title).

    Returns:
        The new session ID.

    Raises:
        ValueError: If turn_index is out of range.
    """
    from kimo_cli.session import Session as KimoCLISession

    wire_path = source_session_dir / "wire.jsonl"
    context_path = source_session_dir / "context.jsonl"

    if turn_index is not None:
        truncated_wire_lines = truncate_wire_at_turn(wire_path, turn_index)
        truncated_context_lines = truncate_context_at_turn(
            context_path, turn_index, wire_path=wire_path
        )
    else:
        # Copy all content
        truncated_wire_lines = _read_all_lines(wire_path)
        truncated_context_lines = _read_all_lines(context_path)

    new_session = await KimoCLISession.create(work_dir=work_dir)
    new_session_dir = new_session.dir

    # Copy referenced video files
    _copy_referenced_videos(source_session_dir, new_session_dir, truncated_wire_lines)

    # Write truncated wire.jsonl
    new_wire_path = new_session_dir / "wire.jsonl"
    with open(new_wire_path, "w", encoding="utf-8") as f:
        for line in truncated_wire_lines:
            f.write(line + "\n")

    # Write truncated context.jsonl (overwrites the empty file from create()).
    # Compaction summaries carried over from the source are stripped of their
    # in-flight-state blocks (current_focus/active_issues/...): those describe
    # the source session's unfinished work, which a fork must not inherit.
    new_context_path = new_session_dir / "context.jsonl"
    with open(new_context_path, "w", encoding="utf-8") as f:
        for line in truncated_context_lines:
            f.write(_strip_compaction_snapshot_line(line) + "\n")

    # Set title and inherit ownership (without owner_id the forked session is
    # filtered out of the session list for authenticated users — looks "killed")
    src_state = load_session_state(source_session_dir)
    if source_title is None:
        source_title = src_state.custom_title or "Untitled"

    fork_title = f"{title_prefix}: {source_title}"

    new_state = load_session_state(new_session_dir)
    new_state.custom_title = fork_title
    new_state.title_generated = True
    new_state.wire_mtime = new_wire_path.stat().st_mtime
    new_state.owner_id = src_state.owner_id
    save_session_state(new_state, new_session_dir)

    return new_session.id


def _read_all_lines(path: Path) -> list[str]:
    """Read all non-empty lines from a file."""
    if not path.exists():
        return []
    lines: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
    return lines


def _copy_referenced_videos(
    source_dir: Path,
    new_session_dir: Path,
    wire_lines: list[str],
) -> None:
    """Copy video files referenced in the wire lines to the new session."""
    source_uploads = source_dir / "uploads"
    if not source_uploads.is_dir():
        return

    referenced_videos: set[str] = set()
    for line in wire_lines:
        for match in re.finditer(r"uploads/([^\"\\<>\s]+)", line):
            fname = match.group(1)
            mime, _ = mimetypes.guess_type(fname)
            if mime and mime.startswith("video/") and "/" not in fname and ".." not in fname:
                referenced_videos.add(fname)

    files_to_copy = [
        source_uploads / name for name in referenced_videos if (source_uploads / name).is_file()
    ]
    if files_to_copy:
        new_uploads = new_session_dir / "uploads"
        new_uploads.mkdir(parents=True, exist_ok=True)
        copied_names: list[str] = []
        for vf in files_to_copy:
            shutil.copy2(vf, new_uploads / vf.name)
            copied_names.append(vf.name)
        (new_uploads / ".sent").write_text(json.dumps(copied_names), encoding="utf-8")
