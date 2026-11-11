"""Self-evolution evidence recorder.

Every completed turn's execution evidence — step count, token usage,
outcome — is appended as one JSON line to ``<session_dir>/evolution/
records.jsonl``. This is the raw material for harness self-improvement
(contrastive Propose–Probe–Compose): the reference execution of every
future improvement attempt comes from here.

Recording is unconditional: evidence collection is a default behaviour of
the runtime, independent of the session's ``self_evolve`` switch, which
governs whether ADOPTED improvements are applied, not whether evidence is
gathered.

Design constraints:
- Evidence collection only. This module never modifies prompts, skills, or
  any other harness component.
- Never raise into the agent loop: recording failures are logged and
  dropped, so evolution can never break a turn.
- Append-only JSONL: cheap, replayable, human-inspectable.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from kimo_cli.utils.logging import logger

EVOLUTION_DIR_NAME = "evolution"
RECORDS_FILE_NAME = "records.jsonl"


@dataclass
class EvolutionTurnRecord:
    """One completed turn's execution evidence."""

    ts: float
    """Unix timestamp when the turn ended."""

    mode: Literal["plan", "agent"]
    """Plan (read-only) or agent mode."""

    outcome: Literal["completed", "cancelled", "failed"]
    """completed: the agent finished the turn on its own."""

    steps: int
    """Number of LLM steps the turn took."""

    duration_ms: int
    """Wall-clock duration of the turn."""

    input_tokens: int
    """Cumulative prompt tokens across the turn's LLM calls."""

    output_tokens: int
    """Cumulative completion tokens across the turn's LLM calls."""

    reasoning_tokens: int
    """Cumulative reasoning tokens across the turn's LLM calls (0 if the
    endpoint does not report them separately)."""

    task: str | None = None
    """The turn's user prompt, truncated to 300 characters. None for slash
    command turns and media-only prompts. Written for every turn so the
    improvement layer can replay real tasks; old records lack this field."""


class EvolutionRecorder:
    """Appends turn records to a session's evolution library."""

    def __init__(self, session_dir: Path) -> None:
        self._records_path = session_dir / EVOLUTION_DIR_NAME / RECORDS_FILE_NAME

    def record_turn(self, record: EvolutionTurnRecord) -> None:
        """Append one record; failures are logged, never raised."""
        try:
            self._records_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._records_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("Failed to record evolution turn evidence", exc_info=True)
