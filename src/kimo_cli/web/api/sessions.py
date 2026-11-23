"""Sessions API routes."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import mimetypes
import os
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from kaos.path import KaosPath
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket, WebSocketDisconnect

from kimo_cli import logger
from kimo_cli.agentspec import discover_user_agent_specs
from kimo_cli.background.status_counts import background_status_counts
from kimo_cli.metadata import load_metadata, save_metadata
from kimo_cli.session import Session as KimoCLISession
from kimo_cli.share import get_share_dir
from kimo_cli.utils.subprocess_env import get_clean_env
from kimo_cli.web.auth import is_origin_allowed, is_private_ip, verify_token
from kimo_cli.web.models import (
    GenerateTitleRequest,
    GenerateTitleResponse,
    GitDiffStats,
    GitFileDiff,
    Session,
    SessionStatus,
    UpdateSessionRequest,
)
from kimo_cli.web.runner.container import ContainerRunner
from kimo_cli.web.runner.messages import new_session_status_message, send_history_complete
from kimo_cli.web.runner.process import KimoCLIRunner
from kimo_cli.web.store.sessions import (
    JointSession,
    invalidate_sessions_cache,
    load_session_by_id,
    load_sessions_page,
    run_auto_archive,
    session_last_activity,
)
from kimo_cli.wire.jsonrpc import (
    ErrorCodes,
    JSONRPCErrorObject,
    JSONRPCErrorResponse,
    JSONRPCInMessageAdapter,
    JSONRPCPromptMessage,
)
from kimo_cli.wire.serde import deserialize_wire_message
from kimo_cli.wire.types import is_request

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
work_dirs_router = APIRouter(prefix="/api/work-dirs", tags=["work-dirs"])
agents_router = APIRouter(prefix="/api/agents", tags=["agents"])

# Constants
MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100MB
DEFAULT_MAX_PUBLIC_PATH_DEPTH = 6
SENSITIVE_PATH_PARTS = {
    "id_rsa",
    "id_ed25519",
    "known_hosts",
    "credentials",
    ".aws",
    ".ssh",
    ".gnupg",
    ".kube",
    ".npmrc",
    ".pypirc",
    ".netrc",
}
SENSITIVE_PATH_EXTENSIONS = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".kdbx",
    ".der",
}
# Home directory patterns to detect if resolved path escapes to sensitive locations
SENSITIVE_HOME_PATHS = {
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube",
}


def sanitize_filename(filename: str) -> str:
    """Remove potentially dangerous characters from filename."""
    # Keep only alphanumeric, dots, underscores, hyphens, and spaces
    safe = "".join(c for c in filename if c.isalnum() or c in "._- ")
    return safe.strip() or "unnamed"


def get_runner(req: Request) -> KimoCLIRunner:
    """Get the KimoCLIRunner from the FastAPI app state."""
    return req.app.state.runner


def get_runner_ws(ws: WebSocket) -> KimoCLIRunner:
    """Get the KimoCLIRunner from the FastAPI app state (for WebSocket routes)."""
    return ws.app.state.runner


def _allowed_work_dir_roots(user: dict[str, Any]) -> list[Path]:
    """Return the directory trees an authenticated deployment exposes to a user.

    ``KIMO_WEB_ALLOWED_WORK_DIRS`` is an ``os.pathsep``-separated allowlist.
    ``KIMO_DEFAULT_WORK_DIR`` remains allowed for backwards compatibility. With
    neither setting, each user gets an isolated workspace below the share dir.
    A ``{user_id}`` placeholder in either setting permits per-user roots.
    """
    configured = os.environ.get("KIMO_WEB_ALLOWED_WORK_DIRS", "")
    root_values = [value for value in configured.split(os.pathsep) if value.strip()]
    default_dir = os.environ.get("KIMO_DEFAULT_WORK_DIR")
    if default_dir:
        root_values.insert(0, default_dir)
    if not root_values:
        root_values.append(str(get_share_dir() / "workspaces" / "{user_id}"))

    user_id = quote(str(user["id"]), safe="")
    return [
        Path(value.replace("{user_id}", user_id)).expanduser().resolve() for value in root_values
    ]


def may_access_work_dir(work_dir: str | Path, user: dict[str, Any] | None) -> bool:
    """Apply directory authorization only to genuinely multi-user identities.

    Anonymous/static-token and ``local_owner`` callers retain the single-user
    directory picker. Admins already have cross-session access and keep it.
    Other authenticated users may select only an allowed root or a descendant;
    this is an application-level boundary, not a reliance on service-account
    OS permissions.
    """
    if user is None or user.get("auth_mode") == "local" or user.get("role") == "admin":
        return True
    path = Path(work_dir).expanduser().resolve()
    return any(path.is_relative_to(root) for root in _allowed_work_dir_roots(user))


def _ensure_work_dir_allowed(work_dir: Path, user: dict[str, Any] | None) -> None:
    if may_access_work_dir(work_dir, user):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "Working directory is outside the allowed roots. "
            "Configure KIMO_WEB_ALLOWED_WORK_DIRS to grant access."
        ),
    )


def ensure_session_access(session: Session, connection: HTTPConnection) -> None:
    """Refuse a session that belongs to somebody else.

    Listing has always filtered by owner. The per-session routes checked
    nothing at all, so in a multi-user deployment any logged-in user who knew
    (or guessed, or once saw) another user's session id could read that user's
    work-dir files, fork the session, retitle it, or delete it.

    A session with no owner stays reachable by a caller who is not logged in —
    single-user installs, static-token deployments, and everything created
    before multi-user existed, where the middleware is the only gate there is.
    A *logged-in* caller does not get that exception: list_sessions already
    hides ownerless sessions from them, so allowing one to be opened by id only
    widened access without giving anybody back their own history.

    404 rather than 403, so the answer does not confirm the id.
    """
    from kimo_cli.web.user_auth import user_from_connection

    try:
        user = user_from_connection(connection)
    except Exception:  # pragma: no cover - defensive
        logger.debug("Session owner lookup failed")
        user = None

    if may_access_session(session, user):
        return

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Session not found",
    )


def may_access_session(session: Session, user: dict[str, Any] | None) -> bool:
    """The access rule itself, so HTTP and the socket cannot drift apart.

    The socket carried its own copy of this and skipped the check entirely for
    an ownerless session — which is exactly the case the HTTP side denies a
    logged-in caller. A session hidden from someone's list was still
    streamable to them by id.
    """
    if user is not None and user.get("role") == "admin":
        return True
    owner_id = getattr(session, "owner_id", None)
    if owner_id is None:
        owner_allowed = user is None
    else:
        owner_allowed = user is not None and user.get("id") == owner_id
    if not owner_allowed:
        return False
    work_dir = getattr(session, "work_dir", None)
    return work_dir is None or may_access_work_dir(work_dir, user)


def get_editable_session(
    session_id: UUID,
    runner: KimoCLIRunner,
    connection: HTTPConnection,
) -> JointSession:
    """Get a session and verify the caller may edit it and it's not busy."""
    session = load_session_by_id(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    ensure_session_access(session, connection)
    # Check if session is busy
    session_process = runner.get_session(session_id)
    if session_process and session_process.is_busy:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session is busy. Please wait for it to complete before modifying.",
        )
    return session


def _relative_parts(path: Path) -> list[str]:
    return [part for part in path.parts if part not in {"", "."}]


def _is_sensitive_relative_path(rel_path: Path) -> bool:
    parts = _relative_parts(rel_path)
    for part in parts:
        if part.startswith("."):
            return True
        if part.lower() in SENSITIVE_PATH_PARTS:
            return True
    return rel_path.suffix.lower() in SENSITIVE_PATH_EXTENSIONS


def _contains_symlink(path: Path, base: Path) -> bool:
    """Check if any component of the path (relative to base) is a symlink."""
    try:
        current = base
        rel_parts = path.relative_to(base).parts
        for part in rel_parts:
            current = current / part
            if current.is_symlink():
                return True
    except (ValueError, OSError):
        return True
    return False


def _is_path_in_sensitive_location(path: Path) -> bool:
    """Check if resolved path points to a sensitive location (e.g., ~/.ssh, ~/.aws)."""
    try:
        home = Path.home()
        if path.is_relative_to(home):
            rel_to_home = path.relative_to(home)
            first_part = rel_to_home.parts[0] if rel_to_home.parts else ""
            if first_part in SENSITIVE_HOME_PATHS:
                return True
    except (ValueError, RuntimeError):
        pass
    return False


def _ensure_public_file_access_allowed(
    rel_path: Path,
    restrict_sensitive_apis: bool,
    max_path_depth: int = DEFAULT_MAX_PUBLIC_PATH_DEPTH,
) -> None:
    if not restrict_sensitive_apis:
        return
    rel_parts = _relative_parts(rel_path)
    if len(rel_parts) > max_path_depth:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Path too deep for public access "
            f"(max depth: {max_path_depth}, current: {len(rel_parts)}).",
        )
    if _is_sensitive_relative_path(rel_path):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access to sensitive files is disabled.",
        )


def _read_wire_lines(wire_file: Path) -> list[str]:
    """Read and parse wire.jsonl into JSONRPC event strings (runs in thread)."""
    result: list[str] = []
    with open(wire_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    continue
                record = cast(dict[str, Any], record)
                record_type = record.get("type")
                if isinstance(record_type, str) and record_type == "metadata":
                    continue
                message_raw = record.get("message")
                if not isinstance(message_raw, dict):
                    continue
                message_raw = cast(dict[str, Any], message_raw)
                message = deserialize_wire_message(message_raw)
                _is_req = is_request(message)
                event_msg: dict[str, Any] = {
                    "jsonrpc": "2.0",
                    "method": "request" if _is_req else "event",
                    "params": message_raw,
                }
                ts = record.get("timestamp")
                if isinstance(ts, (int, float)):
                    event_msg["timestamp"] = ts
                if _is_req:
                    # JSON-RPC requests require a top-level ``id`` so the
                    # client can correlate its response.  Use the request's
                    # own ``id`` field (e.g. ApprovalRequest.id,
                    # QuestionRequest.id).  Note: ``message_raw`` wraps data
                    # as ``{"type": ..., "payload": {...}}`` so the id lives
                    # on the deserialized object, not at the raw dict top level.
                    event_msg["id"] = message.id
                result.append(json.dumps(event_msg, ensure_ascii=False))
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue
    return result


async def replay_history(ws: WebSocket, session_dir: Path) -> None:
    """Replay historical wire messages from wire.jsonl to a WebSocket.

    Raises on failure. Swallowing here would hand the caller a truncated
    replay that it then announced as complete, and the client -- which had
    already emptied its message list when the first replayed frame arrived --
    would keep the gap as its permanent view until the session was reopened.
    The caller decides whether to send ``history_complete``.
    """
    wire_file = session_dir / "wire.jsonl"
    if not await asyncio.to_thread(wire_file.exists):
        return

    lines = await asyncio.to_thread(_read_wire_lines, wire_file)
    for event_text in lines:
        await ws.send_text(event_text)


@router.get("/", summary="List all sessions")
async def list_sessions(
    request: Request,
    runner: KimoCLIRunner = Depends(get_runner),
    limit: int = 100,
    offset: int = 0,
    q: str | None = None,
    archived: bool | None = None,
    all: bool = False,
) -> list[Session]:
    """List sessions with optional pagination and search.

    Args:
        limit: Maximum number of sessions to return (default 100, max 500).
        offset: Number of sessions to skip (default 0).
        q: Optional search query to filter by title or work_dir.
        archived: Filter by archived status.
            - None (default): Only return non-archived sessions.
            - True: Only return archived sessions.
        all: Admin-only flag; when True, return sessions for all users.
    """
    from kimo_cli.web.user_auth import get_current_user as _get_current_user

    if limit <= 0:
        limit = 100
    if limit > 500:
        limit = 500
    if offset < 0:
        offset = 0

    # Run auto-archive in background (throttled internally, runs at most once per 5 minutes)
    await asyncio.to_thread(run_auto_archive)

    current_user = _get_current_user(request)

    # Who this caller may see, decided before the page is cut rather than
    # after. Filtering the returned page paged across everybody's sessions: a
    # user asking for the first 100 got however many of their own happened to
    # fall inside the global first 100, and offset=100 skipped a hundred other
    # people's sessions rather than a hundred of theirs — so their history
    # looked truncated and pages came back empty with more still to come.
    owner_filter: Callable[[str | None], bool] | None = None
    if current_user is not None:
        is_admin = current_user.get("role") == "admin"
        if not is_admin or not all:
            _mine = current_user["id"]

            def _owner_filter(owner: str | None, _mine: str = _mine) -> bool:
                return owner == _mine

            owner_filter = _owner_filter
    else:
        # Unauthenticated (static-token-only) callers may only see ownerless sessions.
        def _ownerless_filter(owner: str | None) -> bool:
            return owner is None

        owner_filter = _ownerless_filter

    sessions = load_sessions_page(
        limit=limit,
        offset=offset,
        query=q,
        archived=archived,
        owner_filter=owner_filter,
    )

    # Resolve every session's background counts in parallel: awaiting one
    # worker-thread round-trip per session serialized up to `limit` waits.
    async def _counts(session_dir: Any) -> tuple[int, int]:
        if session_dir is None:
            return 0, 0
        return await asyncio.to_thread(background_status_counts, Path(session_dir))

    counts = await asyncio.gather(*(_counts(session.session_dir) for session in sessions))

    for session, (background_tasks, background_interrupted) in zip(sessions, counts, strict=True):
        session_process = runner.get_session(session.session_id)
        session.is_running = session_process is not None and session_process.is_running
        if session_process:
            session.status = session_process.status.model_copy(
                update={
                    "background_tasks": background_tasks,
                    "background_interrupted": background_interrupted,
                }
            )
        elif background_tasks or background_interrupted:
            # No live process: surface background state with a synthetic status
            # so the list can still show running or interrupted tasks.
            session.status = SessionStatus(
                session_id=session.session_id,
                state="stopped",
                seq=0,
                worker_id=None,
                reason=None,
                detail=None,
                updated_at=datetime.now(UTC),
                background_tasks=background_tasks,
                background_interrupted=background_interrupted,
            )
        else:
            session.status = None
    return cast(list[Session], sessions)


@router.get("/activity", summary="Stream the run state of the caller's sessions")
async def session_activity(
    request: Request,
    runner: KimoCLIRunner = Depends(get_runner),
) -> StreamingResponse:
    """Server-Sent Events: one ``snapshot`` on connect, then an ``activity`` per change.

    The page streams one session over its socket and polls the list for the
    rest — not at all while hidden — so a session that finished, or stopped to
    ask for an approval, went unnoticed. This carries every change.

    Only the caller's own sessions, as the default session list shows them;
    an admin is not alerted about other people's work. Registered ahead of
    ``/{session_id}``, which would otherwise take ``activity`` for an id.
    ``?token=`` is accepted because ``EventSource`` cannot send headers.
    """
    from kimo_cli.web.user_auth import get_current_user_sse

    user = get_current_user_sse(request)
    mine = user["id"] if user is not None else None
    owners: dict[UUID, bool] = {}

    async def visible(session_id: UUID) -> bool:
        if session_id not in owners:
            session = await asyncio.to_thread(load_session_by_id, session_id)
            owners[session_id] = session is not None and getattr(session, "owner_id", None) == mine
        return owners[session_id]

    # Subscribed before the snapshot is taken, so no change falls between the
    # two; the page drops anything not newer than what it already holds.
    queue = runner.activity.subscribe()

    async def gen():
        try:
            snapshot = [
                activity.model_dump(mode="json")
                for activity in runner.activity_snapshot()
                if await visible(activity.session_id)
            ]
            yield f"data: {json.dumps({'type': 'snapshot', 'sessions': snapshot})}\n\n"
            while True:
                try:
                    activity = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if await visible(activity.session_id):
                    event = {"type": "activity", **activity.model_dump(mode="json")}
                    yield f"data: {json.dumps(event)}\n\n"
        finally:
            runner.activity.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/{session_id}", summary="Get session")
async def get_session(
    session_id: UUID,
    http_request: Request,
    runner: KimoCLIRunner = Depends(get_runner),
) -> Session | None:
    """Get a session by ID."""
    session = load_session_by_id(session_id)
    if session is not None:
        ensure_session_access(session, http_request)
        session_process = runner.get_session(session_id)
        session.is_running = session_process is not None and session_process.is_running
        session.status = session_process.status if session_process else None
    return session


@router.post("/", summary="Create a new session")
async def create_session(
    http_request: Request,
    request: CreateSessionRequest | None = None,
) -> Session:
    """Create a new session."""
    from kimo_cli.web.user_auth import get_current_user as _get_current_user

    current_user = _get_current_user(http_request)
    multi_user = (
        current_user is not None
        and current_user.get("auth_mode") != "local"
        and current_user.get("role") != "admin"
    )

    # Use provided work_dir or default to user's home directory
    if request and request.work_dir:
        work_dir_path = Path(request.work_dir).expanduser().resolve()
        _ensure_work_dir_allowed(work_dir_path, current_user)
        # Validate the directory exists
        if not work_dir_path.exists():
            if request.create_dir:
                # Auto-create the directory
                try:
                    work_dir_path.mkdir(parents=True, exist_ok=True)
                except PermissionError as e:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Permission denied: cannot create directory {request.work_dir}",
                    ) from e
                except OSError as e:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Failed to create directory: {e}",
                    ) from e
            else:
                # Return 404 to indicate directory does not exist
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Directory does not exist: {request.work_dir}",
                )
        if not work_dir_path.is_dir():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Path is not a directory: {request.work_dir}",
            )
        work_dir = KaosPath.unsafe_from_local_path(work_dir_path)
    else:
        default_dir = os.environ.get("KIMO_DEFAULT_WORK_DIR")
        if default_dir:
            if multi_user:
                assert current_user is not None
                default_path = _allowed_work_dir_roots(current_user)[0]
            else:
                default_path = Path(default_dir).expanduser().resolve()
            default_path.mkdir(parents=True, exist_ok=True)
            work_dir = KaosPath.unsafe_from_local_path(default_path)
        elif multi_user:
            # Never default a multi-user caller to the service account's home:
            # doing so would make every service-readable dotfile selectable.
            assert current_user is not None
            default_path = _allowed_work_dir_roots(current_user)[0]
            default_path.mkdir(parents=True, exist_ok=True)
            work_dir = KaosPath.unsafe_from_local_path(default_path)
        else:
            work_dir = KaosPath.unsafe_from_local_path(Path.home())
    # Resolve agent_name → absolute spec path before session is created so we
    # can reject early if the name is unknown.
    agent_spec_path: Path | None = None
    if request is not None and request.agent_name:
        discovered = discover_user_agent_specs(Path(str(work_dir)))
        match = next((d for d in discovered if d.name == request.agent_name), None)
        if match is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown agent: {request.agent_name}",
            )
        agent_spec_path = match.path.resolve()

    # Validate the per-session model override (if any) against the effective
    # model list (config.toml merged with env-provided models). Done before
    # the session is created so a bad model name leaves nothing on disk.
    session_model: str | None = None
    if request is not None and request.model is not None:
        from kimo_cli.web.api.config import get_effective_model_names

        if request.model not in get_effective_model_names():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Model '{request.model}' not found in config",
            )
        session_model = request.model

    kimo_cli_session = await KimoCLISession.create(work_dir=work_dir)
    # Approval policy is opt-in: clients without an interactive approval
    # surface (e.g. IM bridges) request yolo=True explicitly at creation;
    # interactive web UI sessions keep the approval gate untouched.
    if request is not None and request.yolo is not None:
        from kimo_cli.session_state import (
            load_session_state as _load_state,
        )
        from kimo_cli.session_state import (
            save_session_state as _save_state,
        )

        _state = _load_state(kimo_cli_session.dir)
        _state.approval.yolo = request.yolo
        _save_state(_state, kimo_cli_session.dir)
    context_file = kimo_cli_session.dir / "context.jsonl"

    # Persist per-session config (thinking override + model override + agent spec path)
    _cfg: dict[str, Any] = {}
    if request is not None and request.thinking is not None:
        _cfg["thinking"] = request.thinking
    if session_model is not None:
        _cfg["model"] = session_model
    if agent_spec_path is not None:
        _cfg["agent_spec_path"] = str(agent_spec_path)
    if _cfg:
        (kimo_cli_session.dir / "session_config.json").write_text(
            json.dumps(_cfg), encoding="utf-8"
        )

    # Persist owner_id so session list filtering works correctly
    if current_user is not None:
        from kimo_cli.session_state import load_session_state, save_session_state

        _state = load_session_state(kimo_cli_session.dir)
        _state.owner_id = current_user["id"]
        save_session_state(_state, kimo_cli_session.dir)

    invalidate_sessions_cache()
    invalidate_work_dirs_cache()
    return Session(
        session_id=UUID(kimo_cli_session.id),
        title=kimo_cli_session.title,
        last_updated=session_last_activity(kimo_cli_session.dir, context_file),
        is_running=False,
        status=SessionStatus(
            session_id=UUID(kimo_cli_session.id),
            state="stopped",
            seq=0,
            worker_id=None,
            reason=None,
            detail=None,
            updated_at=datetime.now(UTC),
        ),
        work_dir=str(work_dir),
        session_dir=str(kimo_cli_session.dir),
        model=session_model,
    )


class CreateSessionRequest(BaseModel):
    """Create session request."""

    work_dir: str | None = None
    create_dir: bool = False  # Whether to auto-create directory if it doesn't exist
    thinking: bool | None = None  # Per-session thinking override; None = use global config
    agent_name: str | None = None  # Name of a discovered agent spec
    model: str | None = None  # Per-session model override; None = use global default model
    yolo: bool | None = None  # Per-session auto-approve override; None = keep
    # the approval gate (default off)


class ForkSessionRequest(BaseModel):
    """Fork session request."""

    turn_index: int = Field(..., ge=0)  # 0-based, fork includes this turn and all previous turns


class UploadSessionFileResponse(BaseModel):
    """Upload file response."""

    path: str
    filename: str
    size: int


@router.post("/{session_id}/files", summary="Upload file to session")
async def upload_session_file(
    session_id: UUID,
    file: UploadFile,
    http_request: Request,
    runner: KimoCLIRunner = Depends(get_runner),
) -> UploadSessionFileResponse:
    """Upload a file to a session."""
    session = get_editable_session(session_id, runner, http_request)
    session_dir = session.kimo_cli_session.dir
    upload_dir = session_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Read at most the limit plus one byte, so an oversized upload is rejected
    # before the whole body is buffered in memory.
    content = await file.read(MAX_UPLOAD_SIZE + 1)
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)",
        )

    # Generate safe filename
    file_name = str(uuid4())
    if file.filename:
        safe_name = sanitize_filename(file.filename)
        name, ext = os.path.splitext(safe_name)
        file_name = f"{name}_{file_name[:6]}{ext}"

    upload_path = upload_dir / file_name
    upload_path.write_bytes(content)

    return UploadSessionFileResponse(
        path=str(upload_path),
        filename=file_name,
        size=len(content),
    )


@router.post("/{session_id}/ui-diagnostic", summary="Append a UI diagnostic event")
async def append_ui_diagnostic(
    session_id: UUID,
    payload: dict[str, Any],
    http_request: Request,
) -> Response:
    """Append a frontend UI diagnostic event to the session directory.

    The virtualized message list writes a geometry snapshot here when it detects
    blank viewport space while more messages still exist below, so a message-list
    layout problem can be inspected offline afterwards without the reader opening
    a console in the moment. Written to <session_dir>/ui-diagnostic.jsonl so it is
    removed together with the session, and deliberately bypasses the busy check —
    the interesting state is exactly while a turn is streaming.
    """
    session = load_session_by_id(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    ensure_session_access(session, http_request)
    session_dir = session.kimo_cli_session.dir
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "payload": payload,
    }
    line = json.dumps(record, ensure_ascii=False, default=str)

    def _append() -> None:
        with open(session_dir / "ui-diagnostic.jsonl", "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    await asyncio.to_thread(_append)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{session_id}/uploads/{path:path}",
    summary="Get uploaded file from session uploads",
)
async def get_session_upload_file(
    session_id: UUID,
    path: str,
    http_request: Request,
) -> Response:
    """Get a file from a session's uploads directory."""
    session = load_session_by_id(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    ensure_session_access(session, http_request)

    uploads_dir = (session.kimo_cli_session.dir / "uploads").resolve()
    if not uploads_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Uploads directory not found",
        )

    file_path = (uploads_dir / path).resolve()
    if not file_path.is_relative_to(uploads_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid path: path traversal not allowed",
        )

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found",
        )

    media_type, _ = mimetypes.guess_type(file_path.name)
    encoded_filename = quote(file_path.name, safe="")
    return FileResponse(
        file_path,
        media_type=media_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{encoded_filename}",
        },
    )


async def _get_file_from_container(
    container_name: str,
    path: str,
    work_dir: Path,
    restrict_sensitive_apis: bool,
    max_path_depth: int,
) -> Response:
    """Read a file or list a directory from a running session container."""

    # Verify the container is running before attempting to read from it
    check_proc = await asyncio.create_subprocess_exec(
        "docker",
        "inspect",
        "--format={{.State.Running}}",
        container_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await check_proc.communicate()
    if check_proc.returncode != 0 or stdout.decode().strip() != "true":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session container is not running",
        )

    script = b"""import json, os, sys, base64
from pathlib import Path

work_dir = Path(os.environ["_WORK_DIR"]).resolve()
path = os.environ["_PATH"]
requested = work_dir / path
file_path = requested.resolve()

try:
    rel = file_path.relative_to(work_dir)
except ValueError:
    print(json.dumps({"error": "traversal"}))
    sys.exit(0)

restrict = os.environ.get("_RESTRICT") == "true"
max_depth = int(os.environ.get("_MAX_DEPTH", "6"))
sensitive_parts = set(json.loads(os.environ["_SENSITIVE_PARTS"]))
sensitive_exts = set(json.loads(os.environ["_SENSITIVE_EXTS"]))

def is_sensitive(rel_path):
    parts = [p for p in rel_path.parts if p not in ("", ".")]
    for part in parts:
        if part.startswith("."):
            return True
        if part.lower() in sensitive_parts:
            return True
    return rel_path.suffix.lower() in sensitive_exts

if restrict:
    rel_parts = [p for p in rel.parts if p not in ("", ".")]
    if len(rel_parts) > max_depth:
        print(json.dumps({"error": "toodeep"}))
        sys.exit(0)
    if is_sensitive(rel):
        print(json.dumps({"error": "sensitive"}))
        sys.exit(0)
    try:
        current = work_dir
        # Walk the *unresolved* request path: resolve() has already followed
        # any symlink, so checking file_path can never see it.
        for part in requested.relative_to(work_dir).parts:
            current = current / part
            if current.is_symlink():
                print(json.dumps({"error": "symlink"}))
                sys.exit(0)
    except (ValueError, OSError):
        print(json.dumps({"error": "symlink"}))
        sys.exit(0)

if not file_path.exists():
    print(json.dumps({"error": "notfound"}))
    sys.exit(0)

if file_path.is_dir():
    entries = []
    for sub in file_path.iterdir():
        rel_sub = rel / sub.name
        if restrict and is_sensitive(rel_sub):
            continue
        if sub.is_dir():
            entries.append({"name": sub.name, "type": "directory"})
        else:
            try:
                size = sub.stat().st_size
            except OSError:
                size = 0
            entries.append({"name": sub.name, "type": "file", "size": size})
    entries.sort(key=lambda x: (x["type"], x["name"]))
    print(json.dumps({"type": "directory", "entries": entries}))
else:
    data = file_path.read_bytes()
    encoded = base64.b64encode(data).decode()
    print(json.dumps({"type": "file", "name": file_path.name, "data": encoded}))
"""

    proc = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        "-i",
        "-e",
        f"_WORK_DIR={str(work_dir)}",
        "-e",
        f"_PATH={path}",
        "-e",
        f"_RESTRICT={'true' if restrict_sensitive_apis else 'false'}",
        "-e",
        f"_MAX_DEPTH={max_path_depth}",
        "-e",
        f"_SENSITIVE_PARTS={json.dumps(list(SENSITIVE_PATH_PARTS))}",
        "-e",
        f"_SENSITIVE_EXTS={json.dumps(list(SENSITIVE_PATH_EXTENSIONS))}",
        container_name,
        "python",
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(script)

    if proc.returncode != 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read from container: {stderr.decode()}",
        )

    try:
        result = json.loads(stdout.decode())
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Invalid response from container: {e}",
        ) from e

    error = result.get("error")
    if error == "traversal":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid path: path traversal not allowed",
        )
    if error == "notfound":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found",
        )
    if error == "toodeep":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Path too deep for public access (max depth: {max_path_depth}).",
        )
    if error == "sensitive":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access to sensitive files is disabled.",
        )
    if error == "symlink":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Symbolic links are not allowed in public mode.",
        )

    if result.get("type") == "directory":
        return Response(content=json.dumps(result["entries"]), media_type="application/json")

    content = base64.b64decode(result["data"])
    media_type, _ = mimetypes.guess_type(result["name"])
    encoded_filename = quote(result["name"], safe="")
    return Response(
        content=content,
        media_type=media_type or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


@router.get(
    "/{session_id}/files/{path:path}",
    summary="Get file or list directory from session work_dir",
)
async def get_session_file(
    session_id: UUID,
    path: str,
    request: Request,
) -> Response:
    """Get a file or list directory from session work directory."""
    session = load_session_by_id(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    ensure_session_access(session, request)

    runner = request.app.state.runner
    work_dir = Path(str(session.kimo_cli_session.work_dir)).resolve()

    # In container mode, read from the session container when it is alive
    if isinstance(runner, ContainerRunner):
        session_process = runner.get_session(session_id)
        if session_process is not None and session_process.is_alive:
            restrict_sensitive_apis = getattr(request.app.state, "restrict_sensitive_apis", False)
            max_path_depth = (
                getattr(request.app.state, "max_public_path_depth", None)
                or DEFAULT_MAX_PUBLIC_PATH_DEPTH
            )
            return await _get_file_from_container(
                session_process.container_name,
                path,
                work_dir,
                restrict_sensitive_apis,
                max_path_depth,
            )

    requested_path = work_dir / path
    file_path = requested_path.resolve()

    # Check path traversal
    if not file_path.is_relative_to(work_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid path: path traversal not allowed",
        )

    rel_path = file_path.relative_to(work_dir)
    restrict_sensitive_apis = getattr(request.app.state, "restrict_sensitive_apis", False)
    max_path_depth = (
        getattr(request.app.state, "max_public_path_depth", None) or DEFAULT_MAX_PUBLIC_PATH_DEPTH
    )

    # Additional security checks when restricting sensitive APIs
    if restrict_sensitive_apis:
        # Check for symlinks in the path
        if _contains_symlink(requested_path, work_dir):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Symbolic links are not allowed in public mode.",
            )

        # Check if resolved path points to sensitive location
        if _is_path_in_sensitive_location(file_path):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access to sensitive system directories is not allowed.",
            )

    _ensure_public_file_access_allowed(rel_path, restrict_sensitive_apis, max_path_depth)

    if not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found",
        )

    if file_path.is_dir():
        result: list[dict[str, str | int]] = []
        for subpath in file_path.iterdir():
            if restrict_sensitive_apis:
                rel_subpath = rel_path / subpath.name
                if _is_sensitive_relative_path(rel_subpath):
                    continue
            if subpath.is_dir():
                result.append({"name": subpath.name, "type": "directory"})
            else:
                try:
                    size = subpath.stat().st_size
                except OSError:
                    size = 0
                result.append({"name": subpath.name, "type": "file", "size": size})
        result.sort(key=lambda x: (cast(str, x["type"]), cast(str, x["name"])))
        return Response(content=json.dumps(result), media_type="application/json")

    content = file_path.read_bytes()
    media_type, _ = mimetypes.guess_type(file_path.name)
    encoded_filename = quote(file_path.name, safe="")
    return Response(
        content=content,
        media_type=media_type or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


def _update_last_session_id(session: JointSession) -> None:
    """Update last_session_id for the session's work directory."""
    kimo_session = session.kimo_cli_session
    work_dir = kimo_session.work_dir

    metadata = load_metadata()
    work_dir_meta = metadata.get_work_dir_meta(work_dir)

    if work_dir_meta is None:
        work_dir_meta = metadata.new_work_dir_meta(work_dir)

    work_dir_meta.last_session_id = kimo_session.id
    save_metadata(metadata)


@router.delete("/{session_id}", summary="Delete a session")
async def delete_session(
    session_id: UUID,
    http_request: Request,
    runner: KimoCLIRunner = Depends(get_runner),
) -> None:
    """Delete a session."""
    session = get_editable_session(session_id, runner, http_request)
    session_process = runner.get_session(session_id)
    if session_process is not None:
        await session_process.stop()
    await runner.forget_session(session_id)
    wd_meta = session.kimo_cli_session.work_dir_meta
    if wd_meta.last_session_id == str(session_id):
        metadata = load_metadata()
        for wd in metadata.work_dirs:
            if wd.path == wd_meta.path:
                wd.last_session_id = None
                break
        save_metadata(metadata)
    session_dir = session.kimo_cli_session.dir
    if session_dir.exists():
        shutil.rmtree(session_dir)
    invalidate_sessions_cache()


@router.patch("/{session_id}", summary="Update session")
async def update_session(
    session_id: UUID,
    request: UpdateSessionRequest,
    http_request: Request,
    runner: KimoCLIRunner = Depends(get_runner),
) -> Session:
    """Update a session (e.g., rename title, archive/unarchive, or switch model)."""
    from kimo_cli.session_state import load_session_state, save_session_state

    # A model switch requires the session to be idle: reject with 409 while
    # the session is generating a response. Other updates (title/archive)
    # keep the existing get_editable_session 400 behaviour.
    session_process = runner.get_session(session_id)
    if request.model is not None:
        session = load_session_by_id(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found",
            )
        ensure_session_access(session, http_request)
        if session_process and session_process.is_busy:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Session is busy generating a response. "
                "Please wait for it to complete before switching model.",
            )
        # Validate before any writes so a bad model name aborts the whole PATCH.
        from kimo_cli.web.api.config import get_effective_model_names

        if request.model not in get_effective_model_names():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Model '{request.model}' not found in config",
            )
    else:
        session = get_editable_session(session_id, runner, http_request)
    session_dir = session.kimo_cli_session.dir
    state = load_session_state(session_dir)

    # Update title if provided
    if request.title is not None:
        state.custom_title = request.title
        state.title_generated = True

    # Update archived status if provided
    if request.archived is not None:
        state.archived = request.archived
        if request.archived:
            state.archived_at = time.time()
            state.auto_archive_exempt = False
        else:
            state.archived_at = None
            state.auto_archive_exempt = True

    save_session_state(state, session_dir)

    # Update per-session model override if provided
    if request.model is not None:
        cfg_file = session_dir / "session_config.json"
        cfg: dict[str, Any] = {}
        if cfg_file.exists():
            try:
                loaded_cfg: Any = json.loads(cfg_file.read_text(encoding="utf-8"))
                if isinstance(loaded_cfg, dict):
                    cfg = cast("dict[str, Any]", loaded_cfg)
            except Exception:
                cfg = {}
        cfg["model"] = request.model
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

        # Restart only this session's worker (when running and idle) so the
        # new model takes effect; other sessions are untouched.
        if session_process is not None and session_process.is_running:
            await session_process.restart_worker(reason="model_update")

    # Invalidate cache to force reload
    invalidate_sessions_cache()

    # Return updated session
    updated_session = load_session_by_id(session_id)
    if updated_session is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reload session after update",
        )
    return updated_session


def extract_first_turn_from_wire(session_dir: Path) -> tuple[str, str] | None:
    """Extract the first turn's user message and assistant response from wire.jsonl.

    A first turn that was stopped before any reply still has what the person
    asked; the response is then empty. Requiring one left such a session to be
    named after whichever later turn finished first, often a bare "continue".

    Returns:
        tuple[str, str] | None: (user_message, assistant_response) or None if not found
    """
    wire_file = session_dir / "wire.jsonl"
    if not wire_file.exists():
        return None

    user_message: str | None = None
    assistant_response_parts: list[str] = []
    in_first_turn = False

    try:
        with open(wire_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    message = record.get("message", {})
                    msg_type = message.get("type")

                    if msg_type == "TurnBegin":
                        if in_first_turn:
                            # Second turn started, stop
                            break
                        in_first_turn = True
                        user_input = message.get("payload", {}).get("user_input")
                        if user_input:
                            from kosong.message import Message

                            msg = Message(role="user", content=user_input)
                            user_message = msg.extract_text(" ")

                    elif msg_type == "ContentPart" and in_first_turn:
                        payload = message.get("payload", {})
                        if payload.get("type") == "text" and payload.get("text"):
                            assistant_response_parts.append(payload["text"])

                    elif msg_type == "TurnEnd" and in_first_turn:
                        break

                except json.JSONDecodeError:
                    continue
    except OSError:
        return None

    if user_message:
        return (user_message, "".join(assistant_response_parts))
    return None


@router.post("/{session_id}/fork", summary="Fork a session at a specific turn")
async def fork_session_endpoint(
    session_id: UUID,
    request: ForkSessionRequest,
    http_request: Request,
    runner: KimoCLIRunner = Depends(get_runner),
) -> Session:
    """Fork a session, creating a new session with history up to the specified turn.

    The new session shares the same work_dir as the original session.
    """
    from kimo_cli.session_fork import fork_session as do_fork

    source_session = get_editable_session(session_id, runner, http_request)
    source_dir = source_session.kimo_cli_session.dir
    work_dir = source_session.kimo_cli_session.work_dir

    source_title = source_session.title

    try:
        new_session_id = await do_fork(
            source_session_dir=source_dir,
            work_dir=work_dir,
            turn_index=request.turn_index,
            title_prefix="Fork",
            source_title=source_title,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    invalidate_sessions_cache()
    invalidate_work_dirs_cache()

    from kimo_cli.metadata import load_metadata
    from kimo_cli.session_state import load_session_state

    metadata = load_metadata()
    work_dir_meta = metadata.get_work_dir_meta(work_dir)
    assert work_dir_meta is not None
    new_session_dir = work_dir_meta.sessions_dir / new_session_id
    new_state = load_session_state(new_session_dir)
    fork_title = new_state.custom_title or f"Fork: {source_title}"

    context_file = new_session_dir / "context.jsonl"
    return Session(
        session_id=UUID(new_session_id),
        title=fork_title,
        last_updated=session_last_activity(new_session_dir, context_file),
        is_running=False,
        status=SessionStatus(
            session_id=UUID(new_session_id),
            state="stopped",
            seq=0,
            worker_id=None,
            reason=None,
            detail=None,
            updated_at=datetime.now(UTC),
        ),
        work_dir=str(work_dir),
        session_dir=str(new_session_dir),
    )


@router.post("/{session_id}/generate-title", summary="Generate session title using AI")
async def generate_session_title(
    session_id: UUID,
    http_request: Request,
    request: GenerateTitleRequest | None = None,
    runner: KimoCLIRunner = Depends(get_runner),
) -> GenerateTitleResponse:
    """Generate a concise session title using AI based on the first conversation turn.

    If request body is empty or parameters are missing, the backend will
    automatically read the first turn from wire.jsonl.
    """
    session = get_editable_session(session_id, runner, http_request)
    session_dir = session.kimo_cli_session.dir

    from kimo_cli.session_state import load_session_state, save_session_state

    state = load_session_state(session_dir)

    # Check if title was already generated (avoid duplicate calls)
    if state.title_generated:
        return GenerateTitleResponse(title=state.custom_title or "Untitled")

    # Get message content: prefer request parameters, otherwise read from wire.jsonl
    user_message = request.user_message if request else None
    assistant_response = request.assistant_response if request else None

    if not user_message or not assistant_response:
        first_turn = extract_first_turn_from_wire(session_dir)
        if first_turn:
            user_message, assistant_response = first_turn

    # If still no user message, return default title
    if not user_message:
        return GenerateTitleResponse(title="Untitled")

    from kimo_cli.utils.string import fold_message_markup, shorten

    # What the person wrote, not the pasted text and file contents it carries:
    # both the fallback and the model's prompt were built from the raw markup.
    user_message = fold_message_markup(user_message)
    user_text = user_message.strip()
    user_text = " ".join(user_text.split())
    fallback_title = shorten(user_text, width=50) or "Untitled"

    # If AI generation failed too many times, use fallback and mark as generated
    if state.title_generate_attempts >= 3:
        fresh = load_session_state(session_dir)
        # Respect a title finalized by another request/user action while we
        # were preparing a fallback.
        if fresh.title_generated:
            invalidate_sessions_cache()
            return GenerateTitleResponse(title=fresh.custom_title or "Untitled")
        fresh.custom_title = fallback_title
        fresh.title_generated = True
        save_session_state(fresh, session_dir)
        invalidate_sessions_cache()
        return GenerateTitleResponse(title=fallback_title)

    # Try to generate title using AI
    title = fallback_title
    ai_generated = False
    try:
        from kosong import generate
        from kosong.message import Message

        from kimo_cli.auth.oauth import OAuthManager
        from kimo_cli.config import load_config
        from kimo_cli.llm import (
            create_llm,
        )

        config = load_config()
        model_name = config.default_model

        if model_name and model_name in config.models:
            model_config = config.models[model_name]
            provider_config = config.providers.get(model_config.provider)

            if provider_config:
                oauth = OAuthManager(config)
                await oauth.ensure_fresh()
                llm = create_llm(provider_config, model_config, oauth=oauth)

                if llm:
                    system_prompt = (
                        "Generate a concise session title (max 50 characters) "
                        "based on the conversation. "
                        "Only respond with the title text, nothing else. "
                        "No quotes, no explanation."
                    )

                    prompt = f"""User: {user_message[:300]}
Assistant: {(assistant_response or "")[:300]}

Title:"""

                    result = await generate(
                        chat_provider=llm.chat_provider,
                        system_prompt=system_prompt,
                        tools=[],
                        history=[Message(role="user", content=prompt)],
                    )

                    generated_title = result.message.extract_text().strip()
                    # Remove quotes if present
                    generated_title = generated_title.strip("\"'")

                    if generated_title and len(generated_title) <= 50:
                        title = generated_title
                        ai_generated = True
                    elif generated_title:
                        title = shorten(generated_title, width=50)
                        ai_generated = True

    except Exception as e:
        logger.warning(f"Failed to generate title using AI: {e}")
        # Keep fallback_title, ai_generated stays False

    # Read-modify-write: reload fresh state to avoid overwriting
    # worker changes made during the LLM call
    fresh = load_session_state(session_dir)
    # Another request or manual rename may have finalized the title while the
    # LLM call was in flight. Preserve that newer title instead of clobbering it.
    if fresh.title_generated:
        invalidate_sessions_cache()
        return GenerateTitleResponse(title=fresh.custom_title or "Untitled")
    fresh.custom_title = title
    if ai_generated:
        fresh.title_generated = True
    else:
        fresh.title_generate_attempts = fresh.title_generate_attempts + 1
    save_session_state(fresh, session_dir)

    # Invalidate cache
    invalidate_sessions_cache()

    return GenerateTitleResponse(title=title)


# The runner's prompt registration and stdin write are deliberately not changed
# here. Serializing the API-side busy check and send closes the two-socket race
# without growing a lock entry per deleted session: process replacement or
# deletion lets the weak key and its lock be collected.
_session_prompt_locks: WeakKeyDictionary[object, asyncio.Lock] = WeakKeyDictionary()


def _prompt_lock_for(session_process: object) -> asyncio.Lock:
    lock = _session_prompt_locks.get(session_process)
    if lock is None:
        lock = asyncio.Lock()
        _session_prompt_locks[session_process] = lock
    return lock


async def _send_session_prompt(
    session_process: Any,
    websocket: WebSocket,
    in_message: JSONRPCPromptMessage,
    message: str,
) -> bool:
    """Check idle and hand one prompt to the runner as an indivisible API step."""
    async with _prompt_lock_for(session_process):
        if session_process.is_busy and session_process.status.state != "error":
            await websocket.send_text(
                JSONRPCErrorResponse(
                    id=in_message.id,
                    error=JSONRPCErrorObject(
                        code=ErrorCodes.INVALID_STATE,
                        message=(
                            "Session is busy; wait for completion before sending a new prompt."
                        ),
                    ),
                ).model_dump_json()
            )
            return False

        if session_process.status.state == "error":
            logger.info(
                "Clearing stale in-flight prompts for "
                f"session {session_process.session_id} (was in error state)"
            )
            session_process.clear_in_flight()

        await session_process.send_message(message)
        return True


def _websocket_user(websocket: WebSocket) -> dict[str, Any] | None:
    """Resolve the logged-in user behind a WebSocket handshake, if any."""
    from kimo_cli.web.user_auth import user_from_connection

    try:
        return user_from_connection(websocket)
    except Exception:
        logger.debug("WebSocket user lookup failed")
        return None


@router.websocket("/{session_id}/stream")
async def session_stream(
    session_id: UUID,
    websocket: WebSocket,
    runner: KimoCLIRunner = Depends(get_runner_ws),
) -> None:
    """WebSocket stream for a session.

    Flow:
    1. Accept the WebSocket connection
    2. If history exists, attach WebSocket in replay mode
    3. Replay history messages from wire.jsonl
    4. Start worker if needed
    5. Flush buffered live messages and send status snapshot
    6. Forward incoming messages to the subprocess
    7. Clean up on disconnect
    """
    expected_token = getattr(websocket.app.state, "session_token", None)
    enforce_origin = getattr(websocket.app.state, "enforce_origin", False)
    allowed_origins = getattr(websocket.app.state, "allowed_origins", [])
    lan_only = getattr(websocket.app.state, "lan_only", False)

    # LAN-only check
    if lan_only:
        client_ip = websocket.client.host if websocket.client else None
        if client_ip and not is_private_ip(client_ip):
            await websocket.close(code=4403, reason="Access denied: LAN only")
            return

    if enforce_origin:
        origin = websocket.headers.get("origin")
        if origin and not is_origin_allowed(origin, allowed_origins):
            await websocket.close(code=4403, reason="Origin not allowed")
            return

    if expected_token:
        # Accept the same credentials the HTTP side does. AuthMiddleware lets a
        # request through on either the session token *or* a valid kimo_session
        # cookie; requiring only the token here meant that on any install with
        # a session token configured, a logged-in browser got 200 on every API
        # call and a rejected handshake on every socket — which the UI could
        # only report as "lost connection to the session".
        token = websocket.query_params.get("token")
        if not verify_token(token, expected_token) and not _websocket_user(websocket):
            await websocket.close(code=4401, reason="Auth required")
            return

    await websocket.accept()

    # Check if session exists
    session = await asyncio.to_thread(load_session_by_id, session_id)
    if session is None:
        await websocket.close(code=4004, reason="Session not found")
        return

    # And that it is this user's. The handshake above only established that
    # *someone* is logged in; without this, another user's session id was a
    # live stream of their conversation. Same rule as the HTTP routes, from the
    # same function — a socket cannot raise an HTTPException, but it must not
    # answer a different question either.
    if not may_access_session(session, _websocket_user(websocket)):
        await websocket.close(code=4004, reason="Session not found")
        return

    # Check if session has history
    session_dir = session.kimo_cli_session.dir
    wire_file = session_dir / "wire.jsonl"
    has_history = await asyncio.to_thread(wire_file.exists)

    session_process = await runner.get_or_create_session(session_id)
    attached = False
    try:
        # False once the replay aborts: announcing completion would leave the
        # client holding a truncated conversation as its final view.
        replay_ok = True
        if has_history:
            # Attach WebSocket in replay mode before history replay
            await session_process.add_websocket_and_begin_replay(websocket)
            attached = True

            # Replay history
            try:
                await replay_history(websocket, session_dir)
            except Exception:
                replay_ok = False
                # Skip ``history_complete``: the client cleared its message list
                # when the first replayed frame arrived, so reporting success
                # here freezes the gap in place. Without the marker its 15s
                # fallback reconnects and retries from a clean read.
                logger.exception("Failed to replay history")

        # Check if WebSocket is still connected before continuing
        if not replay_ok or not await send_history_complete(websocket):
            logger.debug("WebSocket disconnected during history replay")
            return

        # Start session environment – if anything fails here, send an error
        # status so the client doesn't hang on "Connecting to environment...".
        try:
            # Ensure work_dir exists
            work_dir = Path(str(session.kimo_cli_session.work_dir))
            await asyncio.to_thread(lambda: work_dir.mkdir(parents=True, exist_ok=True))

            if not attached:
                # No history: attach and start worker
                session_process = await runner.get_or_create_session(session_id)
                await session_process.add_websocket_and_begin_replay(websocket)
                attached = True

            assert session_process is not None
            # End replay and start worker
            await session_process.end_replay(websocket)
            await session_process.start()
            await session_process.send_status_snapshot(websocket)
        except Exception as e:
            logger.warning(f"Failed to start session environment: {e}")
            try:
                error_status = SessionStatus(
                    session_id=session_id,
                    state="error",
                    seq=0,
                    worker_id=None,
                    reason="initialization_failed",
                    detail=str(e),
                    updated_at=datetime.now(UTC),
                )
                await websocket.send_text(
                    new_session_status_message(error_status).model_dump_json()
                )
            except Exception:
                pass
            return

        # Track whether we've updated last_session_id for this connection.
        # We defer the update until the first prompt message is actually forwarded,
        # so that merely opening/viewing a session does not change last_session_id.
        last_session_id_updated = False

        # Forward incoming messages to the subprocess
        while True:
            try:
                message = await websocket.receive_text()
                try:
                    in_message = JSONRPCInMessageAdapter.validate_json(message)
                except ValueError:
                    in_message = None

                if isinstance(in_message, JSONRPCPromptMessage):
                    accepted = await _send_session_prompt(
                        session_process, websocket, in_message, message
                    )
                    if not accepted:
                        continue
                    if not last_session_id_updated:
                        await asyncio.to_thread(_update_last_session_id, session)
                        last_session_id_updated = True
                else:
                    logger.debug(f"sending message to session {session_id}")
                    await session_process.send_message(message)
            except WebSocketDisconnect:
                logger.debug("WebSocket disconnected")
                break
            except Exception as e:
                logger.warning(f"WebSocket error: {e.__class__.__name__} {e}")
                break
    finally:
        if attached and session_process:
            await session_process.remove_websocket(websocket)


# Work dirs cache
_work_dirs_cache: list[str] | None = None
_work_dirs_cache_time: float = 0.0
_WORK_DIRS_CACHE_TTL = 30.0  # seconds


def invalidate_work_dirs_cache() -> None:
    """Clear the work dirs cache."""
    global _work_dirs_cache, _work_dirs_cache_time
    _work_dirs_cache = None
    _work_dirs_cache_time = 0.0


def _is_evolution_dir(path: str) -> bool:
    """True for paths under the internal self-evolution data root.

    Evolution artifacts (proposals, verify_runs, etc.) live under
    ``<share>/evolution/``.  They are not user work directories and must
    never surface in the "recent work dirs" picker.
    """
    root = str(get_share_dir() / "evolution")
    return path == root or path.startswith(root + os.sep)


def _is_im_bridge_dir(path: str) -> bool:
    """True for IM-bridge managed workspace directories.

    IM bridge sessions get a per-sender workspace under
    ``<share>/im_bridge/`` (see kimo_cli.web.im.runner.workspace_path).
    These are internal and must not surface in the "recent work dirs"
    picker or be remembered as the startup directory.
    """
    root = str(get_share_dir() / "im_bridge")
    return path == root or path.startswith(root + os.sep)


def _get_work_dirs_sync() -> list[str]:
    """Synchronous helper for get_work_dirs (runs in thread pool)."""
    import time

    global _work_dirs_cache, _work_dirs_cache_time

    # Check cache
    now = time.time()
    if _work_dirs_cache is not None and (now - _work_dirs_cache_time) < _WORK_DIRS_CACHE_TTL:
        return _work_dirs_cache

    # Build fresh list
    metadata = load_metadata()
    work_dirs: list[str] = []
    for wd in metadata.work_dirs:
        # Filter out temporary directories
        if "/tmp" in wd.path or "/var/folders" in wd.path or "/.cache/" in wd.path:
            continue
        # Filter out internal self-evolution artifact directories
        # (verify_runs, proposals, ...) that leaked into metadata.
        if _is_evolution_dir(wd.path):
            continue
        # Filter out IM-bridge managed workspaces (per-sender internal dirs).
        if _is_im_bridge_dir(wd.path):
            continue
        # Verify directory exists
        if Path(wd.path).exists():
            work_dirs.append(wd.path)

    # Update cache
    result = work_dirs[:20]
    _work_dirs_cache = result
    _work_dirs_cache_time = now
    return result


@work_dirs_router.get("/", summary="List available work directories")
async def get_work_dirs() -> list[str]:
    """Get a list of available work directories from metadata."""
    return await asyncio.to_thread(_get_work_dirs_sync)


@work_dirs_router.get("/startup", summary="Get the startup directory")
async def get_startup_dir(request: Request) -> str:
    """Get the last selected directory, falling back to the app default."""
    last_work_dir = load_metadata().last_work_dir
    if (
        last_work_dir
        and not _is_evolution_dir(last_work_dir)
        and not _is_im_bridge_dir(last_work_dir)
        and Path(last_work_dir).is_dir()
    ):
        return last_work_dir
    return request.app.state.startup_dir


class DiscoveredAgent(BaseModel):
    """A discovered agent spec, exposed to the frontend."""

    name: str
    path: str
    source: str


def _discover_agents_sync(work_dir: str | None) -> list[DiscoveredAgent]:
    # No work_dir → only return user-global specs. Pass a non-existent path;
    # discover_user_agent_specs silently skips missing project dirs.
    wd_path = Path(work_dir).expanduser() if work_dir else Path("/nonexistent")
    discovered = discover_user_agent_specs(wd_path)
    return [DiscoveredAgent(name=d.name, path=str(d.path), source=d.source) for d in discovered]


@agents_router.get("/discovered", summary="List discovered agent specs")
async def get_discovered_agents(work_dir: str | None = None) -> list[DiscoveredAgent]:
    """List discovered agent specs from ~/.kimo/agents and optional project dir."""
    return await asyncio.to_thread(_discover_agents_sync, work_dir)


async def _communicate_with_timeout(
    proc: asyncio.subprocess.Process, timeout: float
) -> tuple[bytes, bytes]:
    """Run ``communicate`` with a timeout, killing the child if it expires.

    ``asyncio.wait_for`` cancels the wait but not the process; without this a
    slow git would be left running as an orphan after the timeout.
    """
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise


def _is_app_runtime_path(rel_path: str) -> bool:
    """True if a git-diff path is app-runtime/metadata noise, not user output.

    The git-diff panel is meant to show what a conversation *produced*. Files
    the app maintains on its own (session storage, Office/WPS temp lock files,
    editor/bytecode artifacts) are excluded so the panel does not mix 9 sessions'
    metadata and temp files into the numbers.

    ``git diff --numstat`` quotes paths containing non-ASCII/whitespace (e.g.
    ``"output/.~...pptx"``), so strip surrounding quotes before matching.
    """
    p = rel_path.strip('"')
    if p.startswith("session-data/"):
        return True
    name = p.rsplit("/", 1)[-1]
    if name.startswith(".~"):  # Office/WPS temp lock file
        return True
    if name.endswith((".tmp", ".swp", ".pyc", ".DS_Store")):
        return True
    return "/__pycache__/" in p or p.endswith("/__pycache__")


async def _git_output(work_dir: Path, *args: str) -> bytes:
    """Return stdout from git, or turn a failed command into a visible error."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(work_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=get_clean_env(),
    )
    stdout, stderr = await _communicate_with_timeout(proc, 5.0)
    if proc.returncode != 0:
        detail = os.fsdecode(stderr).strip() or f"exit status {proc.returncode}"
        raise RuntimeError(f"git {args[0]} failed: {detail}")
    return stdout


def _parse_name_status_z(output: bytes) -> list[tuple[str, str]]:
    """Parse ``git diff --name-status -z``, including rename/copy pairs."""
    tokens = iter(output.split(b"\0"))
    changes: list[tuple[str, str]] = []
    for raw_status in tokens:
        if not raw_status:
            continue
        try:
            raw_path = next(tokens)
            change = os.fsdecode(raw_status)
            if change.startswith(("R", "C")):
                raw_path = next(tokens)  # display and merge by the destination
            changes.append((os.fsdecode(raw_path), change))
        except StopIteration as e:
            raise ValueError("Malformed git name-status output") from e
    return changes


def _parse_numstat_z(output: bytes) -> dict[str, tuple[int, int]]:
    """Parse numstat NUL records and merge them by the destination path."""
    tokens = iter(output.split(b"\0"))
    stats: dict[str, tuple[int, int]] = {}
    for record in tokens:
        if not record:
            continue
        parts = record.split(b"\t", 2)
        if len(parts) != 3:
            raise ValueError("Malformed git numstat output")
        raw_add, raw_delete, raw_path = parts
        try:
            if not raw_path:  # rename/copy: empty path, then source, destination
                next(tokens)
                raw_path = next(tokens)
            additions = 0 if raw_add == b"-" else int(raw_add)
            deletions = 0 if raw_delete == b"-" else int(raw_delete)
        except (StopIteration, ValueError) as e:
            raise ValueError("Malformed git numstat output") from e
        stats[os.fsdecode(raw_path)] = (additions, deletions)
    return stats


@router.get("/{session_id}/git-diff", summary="Get git diff stats")
async def get_session_git_diff(session_id: UUID, http_request: Request) -> GitDiffStats:
    """Get git diff stats for the session's work directory."""
    session = load_session_by_id(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    ensure_session_access(session, http_request)

    work_dir = Path(str(session.kimo_cli_session.work_dir))
    if not (work_dir / ".git").exists():
        return GitDiffStats(is_git_repo=False)

    try:
        check_proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "--verify",
            "HEAD",
            cwd=str(work_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=get_clean_env(),
        )
        await _communicate_with_timeout(check_proc, 5.0)
        has_head = check_proc.returncode == 0

        # An unborn HEAD cannot be a diff base. The index is still meaningful:
        # diff it against the empty tree rather than dropping staged files.
        diff_base = ["HEAD", "--"] if has_head else ["--cached", "--"]
        numstat_output, name_status_output, untracked_output = await asyncio.gather(
            _git_output(work_dir, "diff", "--numstat", "-z", "--find-renames", *diff_base),
            _git_output(work_dir, "diff", "--name-status", "-z", "--find-renames", *diff_base),
            _git_output(work_dir, "ls-files", "--others", "--exclude-standard", "-z"),
        )
        numstat = _parse_numstat_z(numstat_output)
        changes = _parse_name_status_z(name_status_output)

        status_map: dict[str, Literal["added", "modified", "deleted", "renamed"]] = {
            "A": "added",
            "C": "added",
            "D": "deleted",
            "R": "renamed",
        }
        files: list[GitFileDiff] = []
        total_add, total_del = 0, 0
        for path, change in changes:
            if _is_app_runtime_path(path):
                continue
            additions, deletions = numstat.get(path, (0, 0))
            total_add += additions
            total_del += deletions
            files.append(
                GitFileDiff(
                    path=path,
                    additions=additions,
                    deletions=deletions,
                    status=status_map.get(change[:1], "modified"),
                )
            )

        for raw_path in untracked_output.split(b"\0"):
            if not raw_path:
                continue
            path = os.fsdecode(raw_path)
            if _is_app_runtime_path(path):
                continue
            files.append(
                GitFileDiff(
                    path=path,
                    additions=0,
                    deletions=0,
                    status="added",
                )
            )

        return GitDiffStats(
            is_git_repo=True,
            has_changes=len(files) > 0,
            total_additions=total_add,
            total_deletions=total_del,
            files=files,
        )
    except TimeoutError:
        return GitDiffStats(is_git_repo=True, error="Git command timed out")
    except Exception as e:
        return GitDiffStats(is_git_repo=True, error=str(e))
