"""Admin API for browsing and hand-editing the global Wiki.

Writes go through `WikiManager`, the same transaction a model's proposal
commits through: the page, the generated index, the audit log and the global
revision change together or not at all. What an admin skips is the model's
admission gate (value, stability, grounding); the page rules still apply.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError

from kimo_cli.web.user_auth import require_admin
from kimo_cli.wiki.initialize import CATEGORY_DIRS
from kimo_cli.wiki.locking import WikiBusyError
from kimo_cli.wiki.manager import WikiManager, parse_log
from kimo_cli.wiki.models import SourceRef
from kimo_cli.wiki.transaction import WikiConflictError, WikiRecoveryRequired

router = APIRouter(prefix="/api/admin/wiki", tags=["admin-wiki"])

_manager: WikiManager | None = None
_manager_lock = threading.Lock()


def _wiki_manager() -> WikiManager:
    """Open the Wiki once per process; every other process shares its files and locks."""
    global _manager
    with _manager_lock:
        if _manager is None:
            try:
                _manager = WikiManager()
            except KeyError as exc:
                # resolve_wiki_root needs OPENKIMO_WIKI_ROOT or OPENKIMO_APP_DATA_DIR.
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="The Knowledge Base is not configured on this server.",
                ) from exc
        return _manager


async def _call[T](fn: Callable[[WikiManager], T]) -> T:
    try:
        # Construct the manager inside the worker thread too: the first call
        # runs a force=True full index rebuild, which would otherwise block
        # the event loop before to_thread is ever reached.
        return await asyncio.to_thread(lambda: fn(_wiki_manager()))
    except WikiConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except WikiBusyError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except WikiRecoveryRequired as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"{exc} {exc.action}"
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"No such page: {exc}") from exc
    except ValidationError as exc:
        message = "; ".join(str(error.get("msg", "")) for error in exc.errors())
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class WikiPageSummaryOut(BaseModel):
    path: str
    category: str
    title: str | None
    tags: list[str]
    created: datetime | None
    updated: datetime | None
    revision: int | None
    source_count: int
    error: str | None


class WikiOverview(BaseModel):
    root: str
    global_revision: int
    categories: list[str]
    pages: list[WikiPageSummaryOut]


class WikiSourceOut(BaseModel):
    kind: Literal["workspace-file", "conversation", "web"]
    label: str
    content_hash: str


class WikiPageDetail(BaseModel):
    path: str
    title: str
    tags: list[str]
    created: datetime
    updated: datetime
    revision: int
    sources: list[WikiSourceOut]
    body: str
    global_revision: int


class WriteWikiPageRequest(BaseModel):
    title: str
    tags: list[str] = Field(default_factory=list)
    body: str
    #: The revision the editor started from; ``None`` creates a new page.
    expected_revision: int | None = None


class WikiWriteResult(BaseModel):
    global_revision: int
    pages: list[str]


class WikiSearchHit(BaseModel):
    path: str
    title: str
    summary: str
    snippet: str
    revision: int


class WikiLintIssueOut(BaseModel):
    code: str
    path: str
    detail: str
    related_paths: list[str]


class WikiLintOut(BaseModel):
    scanned_pages: int
    issues: list[WikiLintIssueOut]


class WikiLogEntry(BaseModel):
    timestamp: str
    operation: str
    revision: int | None
    pages: list[str]
    summary: str


def _source_out(source: SourceRef) -> WikiSourceOut:
    if source.kind == "workspace-file":
        label = f"{source.path} (workspace {source.workspace_id})"
    elif source.kind == "conversation":
        label = f"conversation {source.session_id}"
    else:
        label = str(source.url)
    return WikiSourceOut(kind=source.kind, label=label, content_hash=source.content_hash)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", summary="List Wiki pages (admin only)")
async def get_wiki_overview(
    admin: dict[str, Any] = Depends(require_admin),
) -> WikiOverview:
    def run(manager: WikiManager) -> WikiOverview:
        listing = manager.list_pages()
        return WikiOverview(
            root=str(manager.layout.root),
            global_revision=listing.global_revision,
            categories=list(CATEGORY_DIRS),
            pages=[
                WikiPageSummaryOut(
                    path=page.logical_path,
                    category=page.logical_path.split("/", 1)[0],
                    title=page.title,
                    tags=list(page.tags),
                    created=page.created,
                    updated=page.updated,
                    revision=page.revision,
                    source_count=page.source_count,
                    error=page.error,
                )
                for page in listing.pages
            ],
        )

    return await _call(run)


@router.get("/search", summary="Search Wiki pages (admin only)")
async def search_wiki(
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=20, ge=1, le=50),
    admin: dict[str, Any] = Depends(require_admin),
) -> list[WikiSearchHit]:
    results = await _call(lambda manager: manager.search(q, limit))
    return [
        WikiSearchHit(
            path=result.logical_path,
            title=result.title,
            summary=result.summary,
            snippet=result.snippet,
            revision=result.revision,
        )
        for result in results
    ]


@router.get("/lint", summary="Check Wiki health (admin only)")
async def lint_wiki(
    admin: dict[str, Any] = Depends(require_admin),
) -> WikiLintOut:
    report = await _call(lambda manager: manager.lint(None))
    return WikiLintOut(
        scanned_pages=report.scanned_pages,
        issues=[
            WikiLintIssueOut(
                code=issue.code,
                path=issue.logical_path,
                detail=issue.detail,
                related_paths=list(issue.related_paths),
            )
            for issue in report.issues
        ],
    )


@router.get("/log", summary="Recent Wiki changes (admin only)")
async def get_wiki_log(
    limit: int = Query(default=100, ge=1, le=1000),
    admin: dict[str, Any] = Depends(require_admin),
) -> list[WikiLogEntry]:
    text = await _call(lambda manager: manager.read_log())
    newest_first = reversed(parse_log(text))
    return [
        WikiLogEntry(
            timestamp=entry.timestamp,
            operation=entry.operation,
            revision=entry.revision,
            pages=list(entry.pages),
            summary=entry.summary,
        )
        for entry, _ in zip(newest_first, range(limit), strict=False)
    ]


@router.get("/pages/{path:path}", summary="Read one Wiki page (admin only)")
async def get_wiki_page(
    path: str,
    admin: dict[str, Any] = Depends(require_admin),
) -> WikiPageDetail:
    result = await _call(lambda manager: manager.read(path))
    page = result.page
    return WikiPageDetail(
        path=page.logical_path,
        title=page.title,
        tags=page.tags,
        created=page.created,
        updated=page.updated,
        revision=page.revision,
        sources=[_source_out(source) for source in page.sources],
        body=page.body,
        global_revision=result.global_revision,
    )


@router.put("/pages/{path:path}", summary="Create or edit a Wiki page (admin only)")
async def put_wiki_page(
    path: str,
    body: WriteWikiPageRequest,
    admin: dict[str, Any] = Depends(require_admin),
) -> WikiWriteResult:
    result = await _call(
        lambda manager: manager.save_page(
            logical_path=path,
            title=body.title,
            tags=body.tags,
            body=body.body,
            expected_revision=body.expected_revision,
        )
    )
    return WikiWriteResult(global_revision=result.global_revision, pages=list(result.pages))


@router.delete(
    "/pages/{path:path}",
    summary="Delete a Wiki page (admin only)",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_wiki_page(
    path: str,
    expected_revision: int = Query(ge=1),
    admin: dict[str, Any] = Depends(require_admin),
) -> None:
    await _call(lambda manager: manager.delete_page(path, expected_revision=expected_revision))


__all__ = ["router"]
