"""Admin API for self-evolution: evidence, proposals, verification, adoption,
generation rollback, and the master switch.

Long-running operations (propose, verify) run as a single mutually-exclusive
background job; clients poll GET /jobs/current. The server is a single-process
uvicorn, so a module-level job record is sufficient.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from kimo_cli.evolution import editor, library, verify
from kimo_cli.utils.logging import logger
from kimo_cli.web.user_auth import require_admin

router = APIRouter(prefix="/api/admin/evolution", tags=["admin", "evolution"])


# Evolution ids are path segments: `library.new_id()` emits 12 hex chars, and a
# hex string may start with a digit, so `str.isidentifier()` is too strict here.
# What matters is that the value stays a single segment with no traversal and no
# path separators (`.` / `..` / `a/b` / `a\b` are all rejected).
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _validate_id(value: str) -> str:
    """Validate an identifier used as a path segment to prevent path traversal."""
    if value in (".", "..") or not _ID_PATTERN.fullmatch(value):
        raise HTTPException(status_code=400, detail="Invalid identifier")
    return value


# --- background job (single, mutually exclusive) -----------------------------

_job: dict[str, Any] | None = None
_background_tasks: set[asyncio.Task[None]] = set()


async def _default_spawner(coro: Any) -> None:
    """Run the job detached. Spawner is module-level so tests can substitute an
    inline awaiter: starlette's TestClient cancels stray tasks when a response
    completes, which real uvicorn (plain asyncio, no per-request cancel scope)
    does not do."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# Tests substitute an inline awaiter (see _default_spawner docstring).
_job_spawner = _default_spawner


class JobStatus(BaseModel):
    status: str  # idle | running | succeeded | failed
    kind: str | None = None
    proposal_id: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


async def _start_job(kind: str, proposal_id: str | None, coro: Any) -> JobStatus:
    global _job
    if _job is not None and _job["status"] == "running":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"An evolution job is already running ({_job['kind']}); wait for it to finish.",
        )
    _job = {
        "status": "running",
        "kind": kind,
        "proposal_id": proposal_id,
        "started_at": time.time(),
        "finished_at": None,
        "error": None,
        "result": None,
    }

    async def _run() -> None:
        global _job
        assert _job is not None
        try:
            _job["result"] = await coro
            _job["status"] = "succeeded"
        except asyncio.CancelledError:
            # Server shutdown (or a test client teardown): record and re-raise
            # so cancellation semantics stay intact.
            _job["status"] = "failed"
            _job["error"] = "cancelled"
            raise
        except Exception as exc:
            logger.exception("Evolution job failed: {kind}", kind=kind)
            _job["status"] = "failed"
            _job["error"] = str(exc)
        finally:
            _job["finished_at"] = time.time()

    await _job_spawner(_run())
    return JobStatus(**{k: _job[k] for k in JobStatus.model_fields})


def _gate_config():
    from kimo_cli.config import load_config

    config = load_config()
    try:
        library.require_enabled(config)
    except library.EvolutionGateError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from None
    return config


def _actor(admin: dict[str, Any]) -> str:
    return str(admin.get("username") or admin.get("id") or "admin")


# --- models ------------------------------------------------------------------


class OverviewResponse(BaseModel):
    enabled: bool
    evidence_turns: int
    evidence_tasks: int
    evidence_total_tokens: int
    proposals_pending: int
    adopted_total: int
    generation_current: int
    generation_count: int
    disabled_ids: list[str]


class EvidenceGroup(BaseModel):
    task: str
    runs: int
    failed: int
    avg_steps: float
    avg_total_tokens: float


class EntrySummary(BaseModel):
    id: str
    status: str
    created_at: float | None = None
    rationale: str
    source_tasks: list[str] = []
    editor_model: str | None = None
    has_evidence: bool = False
    enabled: bool = True


class SwitchRequest(BaseModel):
    enabled: bool


class ProposeRequest(BaseModel):
    task_pattern: str | None = None


class VerifyRequest(BaseModel):
    limit: int = 3


# --- endpoints ---------------------------------------------------------------


@router.get("/overview", summary="Self-evolution overview (admin only)")
async def get_overview(admin: dict[str, Any] = Depends(require_admin)) -> OverviewResponse:
    del admin
    from kimo_cli.config import load_config

    records = library.iter_all_session_records()
    groups = library.task_groups(records)
    state = library.load_generations()
    return OverviewResponse(
        enabled=bool(load_config().default_self_evolve),
        evidence_turns=len(records),
        evidence_tasks=len(groups),
        evidence_total_tokens=sum(r.total_tokens for r in records),
        proposals_pending=len(library.list_proposals()),
        adopted_total=len(library.list_adopted()),
        generation_current=int(state.get("current") or 0),
        generation_count=len(state.get("generations") or []),
        disabled_ids=library.disabled_ids(),
    )


@router.get("/evidence", summary="Task evidence groups (admin only)")
async def get_evidence(
    pattern: str | None = None,
    admin: dict[str, Any] = Depends(require_admin),
) -> list[EvidenceGroup]:
    del admin
    records = library.iter_all_session_records()
    groups = library.task_groups(records, pattern=pattern)
    out: list[EvidenceGroup] = []
    for task, turns in groups:
        fails = sum(1 for t in turns if t.outcome != "completed")
        out.append(
            EvidenceGroup(
                task=task,
                runs=len(turns),
                failed=fails,
                avg_steps=sum(t.steps for t in turns) / len(turns),
                avg_total_tokens=sum(t.total_tokens for t in turns) / len(turns),
            )
        )
    return out


def _rationale_excerpt(entry: library.Entry, width: int = 400) -> str:
    p = entry.proposal_md_path
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8", errors="replace")
    if "## Rationale" in text:
        text = text.split("## Rationale", 1)[1]
        text = text.split("##", 1)[0]
    return " ".join(text.split())[:width]


def _entry_summary(entry: library.Entry, disabled: set[str]) -> EntrySummary:
    return EntrySummary(
        id=entry.id,
        status=str(entry.meta.get("status") or ""),
        created_at=entry.meta.get("created_at"),
        rationale=_rationale_excerpt(entry),
        source_tasks=[str(t) for t in (entry.meta.get("source_tasks") or [])],
        editor_model=entry.meta.get("editor_model"),
        has_evidence=(library.verify_runs_dir() / entry.id / "evidence.json").exists(),
        enabled=entry.id not in disabled,
    )


@router.get("/proposals", summary="List pending proposals (admin only)")
async def list_proposals(admin: dict[str, Any] = Depends(require_admin)) -> list[EntrySummary]:
    del admin
    disabled = set(library.disabled_ids())
    return [_entry_summary(e, disabled) for e in library.list_proposals()]


@router.get("/adopted", summary="List adopted improvements (admin only)")
async def list_adopted(admin: dict[str, Any] = Depends(require_admin)) -> list[EntrySummary]:
    del admin
    disabled = set(library.disabled_ids())
    return [_entry_summary(e, disabled) for e in library.list_adopted()]


@router.get("/proposals/{proposal_id}/evidence", summary="Read verification evidence (admin only)")
async def get_proposal_evidence(
    proposal_id: str,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    del admin
    proposal_id = _validate_id(proposal_id)
    path = library.verify_runs_dir() / proposal_id / "evidence.json"
    if not path.exists():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="No verification evidence for this proposal."
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Bad evidence file: {exc}"
        ) from exc


@router.get("/actions", summary="Recent evolution audit actions (admin only)")
async def get_actions(
    limit: int = 20,
    admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    del admin
    return library.read_actions(limit=limit)


@router.post("/switch", summary="Set the self-evolution master switch (admin only)")
async def set_switch(
    body: SwitchRequest,
    admin: dict[str, Any] = Depends(require_admin),
) -> OverviewResponse:
    from kimo_cli.config import load_config, save_config

    config = load_config()
    config.default_self_evolve = body.enabled
    save_config(config)
    library.log_action("switch", _actor(admin), {"enabled": body.enabled})
    return await get_overview(admin={})


@router.post("/proposals", status_code=status.HTTP_202_ACCEPTED, summary="Run propose")
async def run_propose(
    body: ProposeRequest,
    admin: dict[str, Any] = Depends(require_admin),  # noqa: B008
) -> JobStatus:
    config = _gate_config()
    actor = _actor(admin)

    async def _coro() -> dict[str, Any]:
        result = await editor.propose(config, task_pattern=body.task_pattern)
        library.log_action("propose", actor, {"proposal_id": result.entry.id})
        return {"proposal_id": result.entry.id, "rationale": result.rationale}

    return await _start_job("propose", None, _coro())


@router.post(
    "/proposals/{proposal_id}/verify",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run verify",
)
async def run_verify(
    proposal_id: str,
    body: VerifyRequest,
    admin: dict[str, Any] = Depends(require_admin),  # noqa: B008
) -> JobStatus:
    proposal_id = _validate_id(proposal_id)
    config = _gate_config()
    if not (library.proposals_dir() / proposal_id).is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Proposal not found.")
    limit = max(1, min(body.limit, 10))
    actor = _actor(admin)

    async def _coro() -> dict[str, Any]:
        evidence_path = await verify.verify(config, proposal_id, limit=limit)
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        library.log_action(
            "verify",
            actor,
            {"proposal_id": proposal_id, "verdict": evidence["overall_verdict_suggestion"]},
        )
        return {
            "evidence_path": str(evidence_path),
            "overall_verdict_suggestion": evidence["overall_verdict_suggestion"],
            "tasks": len(evidence.get("tasks") or []),
        }

    return await _start_job("verify", proposal_id, _coro())


@router.post("/proposals/{proposal_id}/adopt", summary="Adopt a verified proposal (admin only)")
async def adopt_proposal(
    proposal_id: str,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    proposal_id = _validate_id(proposal_id)
    config = _gate_config()
    del config
    evidence_path = library.verify_runs_dir() / proposal_id / "evidence.json"
    if not evidence_path.exists():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="No verification evidence. Run verify and review evidence.json before adopting.",
        )
    try:
        library.adopt_proposal(proposal_id, evidence_path, actor=_actor(admin))
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return {
        "adopted": proposal_id,
        "generation_current": library.current_generation_n(),
    }


@router.post("/generations/rollback", summary="Roll back one generation (admin only)")
async def rollback(admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    _gate_config()
    try:
        n = library.rollback_generation()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    library.log_action("rollback", _actor(admin), {"generation": n})
    return {"generation_current": n, "applies_to": "newly started sessions"}


@router.post("/generations/advance", summary="Advance one generation (admin only)")
async def advance(admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    _gate_config()
    try:
        n = library.advance_generation()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    library.log_action("advance", _actor(admin), {"generation": n})
    return {"generation_current": n, "applies_to": "newly started sessions"}


@router.post("/adopted/{adopted_id}/toggle", summary="Toggle one adopted entry")
async def toggle_adopted(
    adopted_id: str,
    body: SwitchRequest,
    admin: dict[str, Any] = Depends(require_admin),  # noqa: B008
) -> dict[str, Any]:
    adopted_id = _validate_id(adopted_id)
    _gate_config()
    if not (library.adopted_dir() / adopted_id).is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Adopted entry not found.")
    library.set_adopted_enabled(adopted_id, body.enabled)
    library.log_action("toggle", _actor(admin), {"adopted_id": adopted_id, "enabled": body.enabled})
    return {"adopted_id": adopted_id, "enabled": body.enabled}


@router.get("/jobs/current", summary="Current evolution job status (admin only)")
async def get_job(admin: dict[str, Any] = Depends(require_admin)) -> JobStatus:
    del admin
    if _job is None:
        return JobStatus(status="idle")
    return JobStatus(**{k: _job[k] for k in JobStatus.model_fields})
