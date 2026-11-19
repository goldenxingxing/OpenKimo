"""Dual-run contrastive verification of a proposal.

Each selected task is executed twice headlessly (``kimo --print --quiet``) in
a disposable workspace: once with the current harness (baseline) and once
with the candidate mounted via the ``KIMO_EVOLUTION_PATCH`` /
``KIMO_EVOLUTION_SKILLS_DIR`` env injection channel (same code path the
application channel uses). Per-turn evidence is read back from each run's
fresh session records, so stdout parsing is never needed.

Caveat recorded in every evidence.json: the replay runs in a clean context,
not the original session — the comparison is "baseline vs candidate under
identical clean conditions", not "original run vs rerun".
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kimo_cli.evolution import library
from kimo_cli.utils.logging import logger

CAVEAT = (
    "Replay runs in a clean context (fresh session, no original conversation "
    "history); the comparison is baseline vs candidate under identical clean "
    "conditions. Single-run contrasts are noisy; treat verdicts as pointers "
    "for human review, not proof."
)

# Per-run subprocess wall-clock cap.  A hung `kimo --print --quiet` (LLM
# API stall, network deadlock, runaway tool loop) must not block verify
# forever — wrap proc.wait() with asyncio.wait_for(timeout=VERIFY_TIMEOUT_S)
# and kill the child on expiry.  Generous enough that a normal task run
# almost never trips it.
VERIFY_TIMEOUT_S = 300

# A runner executes one headless task and returns its recorded evidence.
Runner = Callable[[str, dict[str, str], Path, Path], Awaitable["RunRecord"]]


@dataclass
class RunRecord:
    outcome: str = "unknown"
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    duration_ms: int = 0
    note: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_line(cls, line: str) -> RunRecord:
        d = json.loads(line)
        return cls(
            outcome=str(d.get("outcome") or "unknown"),
            steps=int(d.get("steps") or 0),
            input_tokens=int(d.get("input_tokens") or 0),
            output_tokens=int(d.get("output_tokens") or 0),
            reasoning_tokens=int(d.get("reasoning_tokens") or 0),
            duration_ms=int(d.get("duration_ms") or 0),
        )


@dataclass
class TaskEvidence:
    task: str
    baseline: RunRecord = field(default_factory=RunRecord)
    candidate: RunRecord = field(default_factory=RunRecord)
    token_delta_pct: float | None = None
    steps_delta: int | None = None
    verdict_suggestion: str = "inconclusive"


# ---------------------------------------------------------------------------
# Verify-task admission (side-effect gate)
# ---------------------------------------------------------------------------

#: Notification turns replayed as "tasks" are context-dependent nudges, not
#: self-contained work: the headless agent improvises against whatever state
#: it finds.  2026-11-19 incident: a "Background tasks completed" reminder
#: turn was selected for verification; the verify agent read the real
#: workspace's task_progress.md, found the user's compute job dead and
#: relaunched it — a 3.6-day DE optimization, orphaned to launchd, started
#: by a process nobody authorized to run anything.
_SYSTEM_TURN_RE = re.compile(r"<system-reminder>|</system-reminder>")

#: Side-effect intent: launching background / long-running work, or mutating
#: shared state.  A verify run replays the task inside a full-capability
#: headless agent whose default work dir is a disposable directory, but the
#: agent can still cd to any absolute path the task mentions and start
#: processes there.  These tasks are fine for a supervised session and
#: wrong for an unattended replay.
_SIDE_EFFECT_RE = re.compile(
    r"\b(nohup|setsid|disown|start_new_session|run_in_background|launchd|"
    r"daemon(?:ize)?|hyperparameter(?:\s+search)?|grid\s*search)\b"
    r"|后台(?:运行|执行|启动|挂)|守护进程|脱离会话|"
    r"寻优|批量启动|启动[^。\n]{0,20}(?:寻优|训练|仿真|模拟|计算|脚本)|"
    r"部署|发版|git\s+push",
    re.IGNORECASE,
)

#: Context-dependent prompts the replayed agent will interpret against a
#: clean session and improvise — the improvisation is where the side effects
#: come from.
_CONTEXT_DEPENDENT_RE = re.compile(
    r"^(?:继续|接着|继续吧|continue|go\s*on|resume)[。!\.\s]*$|"
    r"^(?:好|好的|嗯|ok|okay)[，,。!\.\s]*$",
    re.IGNORECASE,
)


def is_verify_safe_task(task: str) -> bool:
    """Whether a recorded task may be replayed by the verify subprocess.

    Conservative text-level gate (R3): reject notification turns, prompts
    with background-launch / long-running / shared-state-mutation intent,
    and context-dependent nudges.  This is an admission filter, not a
    sandbox — process-level isolation of the verify run is a separate layer.
    """
    text = (task or "").strip()
    if not text:
        return False
    if _SYSTEM_TURN_RE.search(text):
        return False
    if _CONTEXT_DEPENDENT_RE.match(text):
        return False
    return not _SIDE_EFFECT_RE.search(text)


def select_tasks(
    config,
    *,
    task_pattern: str | None = None,
    limit: int = 3,
) -> list[str]:
    """Pick replayable tasks: proposal-targeted, failed-heavy first.

    Tasks failing the side-effect gate (``is_verify_safe_task``) are skipped:
    an unattended verify run must never re-execute work that launches
    background processes or mutates shared state.  When every candidate is
    filtered out the result is empty (the caller surfaces "no replayable
    tasks") rather than falling back to an unsafe task.
    """
    del config  # reserved for future weighting; selection is evidence-driven
    records = library.iter_all_session_records()
    groups = library.task_groups(records, pattern=task_pattern)
    tasks: list[str] = []
    for task, turns in groups:
        if not any(t.outcome == "completed" for t in turns):
            # Never replayable to a meaningful baseline skip: keep failed-only
            # tasks only if nothing better exists; they still exercise the path.
            continue
        if not is_verify_safe_task(task):
            logger.info("Verify task rejected by side-effect gate: {task}", task=task[:60])
            continue
        tasks.append(task)
        if len(tasks) >= limit:
            break
    return tasks


def _records_snapshot() -> dict[Path, float]:
    snap: dict[Path, float] = {}
    for root in library.session_record_roots():
        if not root.is_dir():
            continue
        for p in root.rglob("records.jsonl"):
            try:
                snap[p] = p.stat().st_mtime
            except OSError:
                continue
    return snap


def _read_newest_record(snap: dict[Path, float], t0: float) -> RunRecord | None:
    candidates: list[tuple[float, Path]] = []
    for root in library.session_record_roots():
        if not root.is_dir():
            continue
        for p in root.rglob("records.jsonl"):
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if p in snap and snap[p] >= mtime:
                continue
            if mtime < t0 - 1.0:  # clock slack
                continue
            candidates.append((mtime, p))
    if not candidates:
        return None
    _, path = max(candidates)
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    try:
        return RunRecord.from_line(lines[-1])
    except (json.JSONDecodeError, ValueError):
        return None


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the child's whole process group, falling back to the child alone.

    The verify child runs with ``start_new_session=True`` (or
    ``CREATE_NEW_PROCESS_GROUP`` on Windows), so everything the agent spawned
    inside it shares the group. Killing only the direct child would orphan
    long-running grandchildren (e.g. evaluation scripts) to launchd.
    """
    if os.name == "nt":
        proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


async def _subprocess_runner(
    task: str, env_extra: dict[str, str], cwd: Path, out_file: Path
) -> RunRecord:
    """Default runner: one headless ``kimo --print --quiet`` invocation.

    Sets ``KIMO_EVOLUTION_SUBPROCESS=1`` in the child's env so the child's
    own turn-end hook (see ``evolution/auto_evolve.py``) skips its dispatch.
    Without this, one verify would spawn two subprocesses that each
    completed a turn and each triggered their own auto-evolve cycle — a
    verify storm, potentially unbounded.
    """
    cwd.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(env_extra)
    env["KIMO_EVOLUTION_SUBPROCESS"] = "1"
    snap = _records_snapshot()
    t0 = time.time()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    # Run the child in its own process group so a timeout can kill the WHOLE
    # group. `proc.kill()` alone only kills the `kimo --print` child; any
    # long-running grandchildren the agent spawned inside it (evaluation /
    # simulation scripts) would be orphaned to launchd and keep running.
    popen_kwargs: dict = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
    with open(out_file, "wb") as out:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "kimo_cli",
            "--print",
            "--quiet",
            "-p",
            task,
            "--work-dir",
            str(cwd),
            stdout=out,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
            env=env,
            **popen_kwargs,
        )
        try:
            # Timeout guard: a hung subprocess (LLM API stall, network
            # deadlock, runaway tool loop) must not block verify forever.
            # Without this, auto_evolve would never return and the admin
            # panel would have no way to detect the stall.
            await asyncio.wait_for(proc.wait(), timeout=VERIFY_TIMEOUT_S)
        except TimeoutError:
            logger.warning(
                "Verify subprocess timed out after {s}s, killing: {task}",
                s=VERIFY_TIMEOUT_S,
                task=task[:60],
            )
            _kill_process_group(proc)
            with contextlib.suppress(Exception):
                await proc.wait()
            return RunRecord(note=f"verify_timeout ({VERIFY_TIMEOUT_S}s)")
    record = _read_newest_record(snap, t0)
    if record is None:
        logger.warning("No evolution record found for verify run: {task}", task=task[:60])
        return RunRecord(note=f"record_missing (exit code {proc.returncode})")
    return record


def compare(task: str, baseline: RunRecord, candidate: RunRecord) -> TaskEvidence:
    ev = TaskEvidence(task=task, baseline=baseline, candidate=candidate)
    if baseline.note or candidate.note:
        ev.verdict_suggestion = "inconclusive"
        return ev

    completed_b = baseline.outcome == "completed"
    completed_c = candidate.outcome == "completed"
    if completed_b != completed_c:
        ev.verdict_suggestion = "candidate_looks_better" if completed_c else "baseline_looks_better"
        return ev

    ev.token_delta_pct = round(
        (candidate.total_tokens - baseline.total_tokens) / max(baseline.total_tokens, 1) * 100, 1
    )
    ev.steps_delta = candidate.steps - baseline.steps
    if not completed_b:
        ev.verdict_suggestion = "inconclusive"  # both failed: nothing to conclude
    elif ev.token_delta_pct <= -5 or ev.steps_delta < 0:
        ev.verdict_suggestion = "candidate_looks_better"
    elif ev.token_delta_pct >= 5 and ev.steps_delta > 0:
        ev.verdict_suggestion = "baseline_looks_better"
    else:
        ev.verdict_suggestion = "inconclusive"
    return ev


def _overall_verdict(task_evidences: list[TaskEvidence]) -> str:
    votes = [t.verdict_suggestion for t in task_evidences]
    if not votes:
        return "inconclusive"
    for verdict in ("candidate_looks_better", "baseline_looks_better"):
        if votes.count(verdict) > len(votes) / 2:
            return verdict
    return "inconclusive"


def _resolve_entry_dir(proposal_id: str) -> Path:
    """Resolve a proposal directory, refusing ids that leave the proposals root.

    ``proposal_id`` arrives from the admin HTTP API and from the CLI, so it must
    not be trusted as a path segment. ``proposals/..`` is a directory (the
    evolution root), and the retention ``rmtree`` below would then delete every
    proposal, adopted entry and generation; ``proposals/.`` resolves to the
    proposals root itself. Requiring exactly one ordinary path component keeps
    both the read and the ``verify_runs/<id>`` write inside their roots.
    """
    parts = Path(proposal_id).parts
    if len(parts) != 1 or parts[0] in (".", ".."):
        raise ValueError(f"Invalid proposal id: {proposal_id!r}")

    root = library.proposals_dir().resolve()
    entry_path = (root / parts[0]).resolve()
    if entry_path == root or not entry_path.is_relative_to(root):
        raise ValueError(f"Invalid proposal id: {proposal_id!r}")
    if not entry_path.is_dir():
        raise FileNotFoundError(f"Proposal not found: {entry_path}")
    return entry_path


async def verify(
    config,
    proposal_id: str,
    *,
    task_pattern: str | None = None,
    limit: int = 3,
    runner: Runner | None = None,
) -> Path:
    """Dual-run a proposal against real tasks; write evidence.json."""
    library.require_enabled(config)

    entry_path = _resolve_entry_dir(proposal_id)

    tasks = select_tasks(config, task_pattern=task_pattern, limit=limit)
    if not tasks:
        raise RuntimeError(
            "No completed replayable tasks found in the evidence."
            + (f" (pattern {task_pattern!r})" if task_pattern else "")
        )

    candidate_env: dict[str, str] = {}
    append_path = entry_path / "system_append.md"
    skills_path = entry_path / "skills"
    if append_path.exists():
        candidate_env[library.PATCH_ENV_VAR] = str(append_path)
    if skills_path.is_dir():
        candidate_env[library.SKILLS_ENV_VAR] = str(skills_path)

    run = runner or _subprocess_runner
    runs_root = library.verify_runs_dir() / proposal_id
    # Retention: keep only the latest verification run per proposal.
    shutil.rmtree(runs_root, ignore_errors=True)
    runs_root.mkdir(parents=True, exist_ok=True)

    task_evidences: list[TaskEvidence] = []
    for task in tasks:
        tag = hashlib.sha1(task.encode("utf-8")).hexdigest()[:10]
        base_dir = runs_root / tag / "baseline"
        cand_dir = runs_root / tag / "candidate"
        base_out = runs_root / tag / "baseline.out"
        cand_out = runs_root / tag / "candidate.out"
        logger.info("Verify dual-run: {task}", task=task[:60])
        baseline = await run(task, {}, base_dir, base_out)
        candidate = await run(task, candidate_env, cand_dir, cand_out)
        task_evidences.append(compare(task, baseline, candidate))

    evidence = {
        "proposal_id": proposal_id,
        "generated_at": time.time(),
        "caveat": CAVEAT,
        "candidate_env": sorted(candidate_env.keys()),
        "overall_verdict_suggestion": _overall_verdict(task_evidences),
        "tasks": [asdict(t) for t in task_evidences],
    }
    evidence_path = runs_root / "evidence.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return evidence_path
