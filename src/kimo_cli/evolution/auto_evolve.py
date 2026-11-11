"""Auto-evolve: on-every-turn trigger for the propose → verify pipeline.

Design contract
---------------
- Global switch (``config.default_self_evolve``) is the only on/off gate.
  OFF  -> nothing runs (evidence is still collected, always on).
  ON   -> one turn-end hook may schedule a propose+verify cycle.
- Adoption is human-only. ``auto_evolve`` never calls
  ``library.adopt_proposal``.
- Nothing is automatic when the switch is off: no propose, no verify, no
  verify-runs side effects.

Safety nets
-----------
1. Recursion guard.  ``verify.verify`` spawns ``kimo --print --quiet``
   subprocesses for each baseline/candidate run.  Those subprocesses also
   call ``record_turn`` and would otherwise schedule their own auto-evolve
   cycle after completing — turning one verify into a verify storm.  We
   solve this by setting ``KIMO_EVOLUTION_SUBPROCESS=1`` in the
   subprocess's env (see ``verify._subprocess_runner``); the dispatch
   here checks that env var and skips silently if it's set.

2. Cooldown.  A single auto-evolve cycle can cost tens of MB of tokens
   (editor call + up to 3 tasks × 2 sides of a full agent subprocess
   each).  Cooldown prevents a bursty conversation from spawning several
   back-to-back cycles.  Measured wall-clock, persisted in
   ``<evolution>/auto_evolve_state.json`` so a restart respects the
   window.

3. In-flight guard.  Only one cycle runs at a time per process; a second
   dispatch attempt inside the cooldown OR while a task is running is a
   silent no-op (no queued backlog).

4. Minimum evidence.  Editor calls on empty or trivial libraries are
   wasted tokens.  Require at least
   ``evolution_auto_min_evidence`` completed turns before proposing.

Pipeline
--------
::

    propose (one editor LLM call)
        -> proposals/<id>/{PROPOSAL.md, system_append.md, skills/, meta.json}
    verify (limit * 2 full-agent subprocess runs)
        -> verify_runs/<id>/<sha1(task)>/{baseline,candidate}/
        -> verify_runs/<id>/evidence.json
    write meta.json: auto_verified_at, auto_verdict
    audit line: log_action("auto_evolve_cycle", actor="auto", ...)

``meta.json`` gains ``auto_verified_at`` and ``auto_verdict`` so the
Admin panel can distinguish auto-produced candidates from human-triggered
ones when a human goes to click Adopt.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from kimo_cli.evolution import editor, library, verify
from kimo_cli.share import get_share_dir
from kimo_cli.utils.logging import logger

#: Env var set by verify's subprocess runner to prevent the recursion loop.
SUBPROCESS_ENV_VAR = "KIMO_EVOLUTION_SUBPROCESS"

STATE_FILE_NAME = "auto_evolve_state.json"

DEFAULT_MIN_EVIDENCE = 3
DEFAULT_COOLDOWN_S = 60 * 30  # 30 minutes


@dataclass
class AutoEvolveResult:
    """Outcome of one auto-evolve dispatch.

    ``attempted`` is False when a gate kept us from starting at all
    (switch off, cooldown, in-flight, subprocess, min-evidence).  True
    when the cycle actually ran and either produced a proposal or
    recorded a cycle-level error.
    """

    attempted: bool = False
    skip_reason: str | None = None
    proposal_id: str | None = None
    verified: bool = False
    verdict: str | None = None
    error: str | None = None
    duration_ms: int = 0


def _iter_all_records(work_dir: Path | None = None) -> list:
    """Aggregate evolution records across all session roots.

    Sessions land in different places depending on how the agent was
    invoked:

    - App UI sessions: ``<share_dir>/sessions/``.
    - CLI / workspace sessions: ``<work_dir>/session-data/`` (and older
      workspaces also ``<work_dir>/.kimo/sessions/``).

    The library helpers (``editor.propose``, ``verify.select_tasks``) only
    look under ``<share_dir>/sessions/`` today, which misses CLI sessions
    entirely.  ``_iter_all_records`` fixes this in the auto-evolve path.
    The library helpers themselves should get the same treatment as a
    follow-up so propose/verify invoked from the Admin panel also see the
    full library.

    Roots are disjoint by construction, so no cross-root dedupe is
    needed.  Within one records.jsonl, each line is a separate
    TurnEvidence — ``library.iter_session_records`` returns one row per
    line, and we forward them as-is.
    """
    roots: list[Path] = []
    if work_dir is not None:
        # work_dir may be a KaosPath (``session.work_dir`` is a KaosPath);
        # KaosPath has no ``rglob`` and its ``is_dir`` is async, which made
        # iter_session_records blow up with
        #   AttributeError: 'KaosPath' object has no attribute 'rglob'
        # (seen in auto_evolve_state.json last_error).  Normalise to a local
        # Path first so the downstream rglob works.
        wd = Path(str(work_dir))
        roots.append(wd / "session-data")
        roots.append(wd / ".kimo" / "sessions")
    roots.append(get_share_dir() / "sessions")

    out = []
    for root in roots:
        if not root.is_dir():
            continue
        out.extend(library.iter_session_records(root))
    return out


def _min_evidence(config) -> int:
    n = getattr(config, "evolution_auto_min_evidence", DEFAULT_MIN_EVIDENCE)
    try:
        return max(1, int(n))
    except (TypeError, ValueError):
        return DEFAULT_MIN_EVIDENCE


def _cooldown_s(config) -> int:
    n = getattr(config, "evolution_auto_cooldown_s", DEFAULT_COOLDOWN_S)
    try:
        return max(0, int(n))
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_S


# --- State file -------------------------------------------------------------


def _state_path() -> Path:
    return library.evolution_dir() / STATE_FILE_NAME


def _load_state() -> dict:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Bad auto_evolve_state.json, resetting")
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    from kimo_cli.utils.io import atomic_json_write

    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(state, p)
    except OSError:
        logger.warning("Failed to persist auto_evolve_state.json", exc_info=True)


# --- Core cycle -------------------------------------------------------------


async def _run_cycle(config, work_dir: Path | None = None) -> AutoEvolveResult:
    """One propose + verify + annotate cycle.  Raises on unexpected error."""

    started = time.time()
    result = AutoEvolveResult(attempted=True)

    # Preflight evidence depth — refuse to burn tokens on thin libraries.
    records = _iter_all_records(work_dir)
    completed = sum(1 for r in records if r.outcome == "completed" and r.task)
    threshold = _min_evidence(config)
    if completed < threshold:
        result.error = f"insufficient_evidence: {completed} completed turns < {threshold} required"
        result.attempted = False
        result.skip_reason = result.error
        return result

    # Step 1: propose.
    prop = await editor.propose(config, limit=5)
    result.proposal_id = prop.entry.id
    logger.info("Auto-evolve proposed {id}", id=result.proposal_id)

    # Step 2: verify.
    evidence_path = await verify.verify(config, prop.entry.id, limit=3)
    result.verified = True
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        result.verdict = str(evidence.get("overall_verdict_suggestion") or "")
    except (OSError, json.JSONDecodeError):
        result.verdict = None

    # Step 3: annotate meta.json so the Admin panel can flag this as
    # auto-produced.
    entry = prop.entry
    meta = dict(entry.meta)
    meta["auto_verified_at"] = time.time()
    meta["auto_verdict"] = result.verdict
    library.save_meta(entry.path, meta)

    # Step 4: audit.
    duration_ms = int((time.time() - started) * 1000)
    result.duration_ms = duration_ms
    library.log_action(
        "auto_evolve_cycle",
        actor="auto",
        detail={
            "proposal_id": result.proposal_id,
            "verdict": result.verdict,
            "duration_ms": duration_ms,
            "evidence_path": str(evidence_path),
        },
    )
    return result


# --- Public entry points ----------------------------------------------------


_ACTIVE_TASKS: set[asyncio.Task] = set()


def _is_active() -> bool:
    return any(not t.done() for t in _ACTIVE_TASKS)


async def auto_evolve_maybe(config, work_dir: Path | None = None) -> AutoEvolveResult:
    """Try to run one auto-evolve cycle.  Never raises into the caller.

    ``work_dir`` is optional; when supplied, the preflight counts
    workspace session-data records as well as ``<share_dir>/sessions/``
    records.  See ``_iter_all_records`` for the multi-root rationale.

    Every gate is expressed as a skip_reason and folded into the returned
    result, so the caller can inspect what happened without needing a
    try/except.
    """
    result = AutoEvolveResult(attempted=False)
    try:
        # Gate checks run top-to-bottom; the first one to trip wins.
        # They use if/elif (not early return) so the state-persistence
        # block at the bottom always runs — including on skip — so the
        # Admin panel / log can see that we considered a cycle.
        if os.environ.get(SUBPROCESS_ENV_VAR):
            result.skip_reason = "subprocess"
        elif not getattr(config, "default_self_evolve", False):
            result.skip_reason = "switch_off"
        else:
            state = _load_state()
            last_end = float(state.get("last_cycle_end") or 0.0)
            cooldown = _cooldown_s(config)
            remaining = cooldown - (time.time() - last_end)
            if remaining > 0:
                result.skip_reason = f"cooldown({int(remaining)}s_remaining)"
            else:
                try:
                    result = await _run_cycle(config, work_dir)
                except library.EvolutionGateError:
                    # Defensive: gated upstream already, but keep for safety.
                    result.error = "self_evolution_gated"
                except Exception as exc:
                    result.error = f"{type(exc).__name__}: {exc}"
                    logger.warning("Auto-evolve cycle failed: {err}", err=result.error)
    except Exception as exc:
        # Outer catch: any unexpected failure becomes a logged result,
        # never a raised exception.  The recorder never breaks a turn.
        result.skip_reason = "dispatch_error"
        result.error = f"{type(exc).__name__}: {exc}"
        logger.warning("Auto-evolve dispatch crashed: {err}", err=result.error)

    # Persist state even on skip (records the "we considered" moment).
    try:
        state = _load_state()
        state["last_dispatch_ts"] = time.time()
        state["last_skip_reason"] = result.skip_reason
        state["last_error"] = result.error
        if result.proposal_id:
            history = state.setdefault("proposal_history", [])
            history.append(
                {
                    "proposal_id": result.proposal_id,
                    "verified": result.verified,
                    "verdict": result.verdict,
                    "ts": time.time(),
                    "duration_ms": result.duration_ms,
                }
            )
            # Keep the tail bounded.
            if len(history) > 200:
                del history[:-200]
        # Advance `last_cycle_end` ONLY when a cycle actually ran.
        # Setting it on skip paths (subprocess, switch_off, cooldown,
        # insufficient_evidence) would reopen the cooldown window on every
        # turn-end dispatch and permanently starve auto_evolve after the
        # first completed cycle.
        # A failed cycle consumed the same propose+verify budget as a
        # successful one. Without advancing the cooldown here, an exception
        # makes the next turn retry immediately and burn it again.
        if (result.attempted and not result.skip_reason) or result.error:
            state["last_cycle_end"] = time.time()
        _save_state(state)
    except Exception as exc:
        logger.warning("Auto-evolve failed to persist state: {err}", err=str(exc))

    return result


def schedule_auto_evolve(config, work_dir: Path | None = None) -> None:
    """Fire-and-forget dispatch of ``auto_evolve_maybe``.

    Safe to call from any running event loop.  Skips silently when:
    - no running event loop (unit tests, tool-only turns),
    - a previous auto-evolve task is still running.

    The spawned task never raises into the caller: any failure inside is
    caught by ``auto_evolve_maybe`` and folded into its result.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _is_active():
        return

    async def _wrapper() -> None:
        try:
            await auto_evolve_maybe(config, work_dir)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Auto-evolve task crashed: {err}", err=str(exc))

    task = loop.create_task(_wrapper())
    _ACTIVE_TASKS.add(task)
    task.add_done_callback(_ACTIVE_TASKS.discard)


def active_task_count() -> int:
    """Diagnostic: number of currently running auto-evolve tasks."""
    return sum(1 for t in _ACTIVE_TASKS if not t.done())


def get_state() -> dict:
    """Diagnostic: current auto_evolve_state.json contents."""
    return _load_state()
