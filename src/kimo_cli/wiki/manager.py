"""Application service boundary for the shared, authoritative global Wiki."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock

from kimo_cli.wiki.initialize import CATEGORY_DIRS, WikiLayout, ensure_wiki
from kimo_cli.wiki.lint import LintReport, lint_snapshot
from kimo_cli.wiki.locking import WikiLock
from kimo_cli.wiki.models import (
    CurrentSource,
    PageChange,
    PageDeletion,
    SourceRef,
    WikiCandidate,
    WikiPage,
)
from kimo_cli.wiki.schema import (
    content_hash,
    parse_page,
    render_page,
    resolve_page_path,
    validate_logical_page,
)
from kimo_cli.wiki.search import SearchResult, WikiSearchIndex, bounded_markdown_search
from kimo_cli.wiki.transaction import (
    WikiConflictError,
    WikiTransaction,
    acknowledge_reindex,
    wiki_read_lock,
)
from kimo_cli.wiki.value_gate import (
    DiscardedCandidate,
    WikiContext,
    contains_sensitive_text,
    duplicate_candidate_paths,
    evaluate_candidate,
)
from kimo_cli.wiki.workspaces import WorkspaceRegistry

_LOCK_TIMEOUT_SECONDS = 5.0
_CATEGORY_LABELS = {
    "entities": "Entities",
    "concepts": "Concepts",
    "comparisons": "Comparisons",
    "sources": "Sources",
    "queries": "Queries",
    "lint": "Lint",
}


class WikiRevisionMismatch(ValueError):
    """A proposed page change names the wrong base revision for its page.

    A correctable request, not a failure of the Wiki: the message says which
    revision to use, so the same knowledge can be sent again at once.
    """

    def __init__(self, logical_path: str, expected: int | None, current: int | None) -> None:
        self.logical_path = logical_path
        self.expected = expected
        self.current = current
        if current is None:
            message = (
                f"{logical_path} does not exist yet; leave out expected_revision to create it."
            )
        elif expected is None:
            message = (
                f"{logical_path} already exists at revision {current}. To replace it, "
                f"set expected_revision: {current} on that page; to keep both, write "
                "to a new path."
            )
        else:
            message = (
                f"{logical_path} is at revision {current}, not {expected}. If the "
                f"update still applies, set expected_revision: {current}."
            )
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class WikiReadResult:
    page: WikiPage
    content: str
    global_revision: int


@dataclass(frozen=True, slots=True)
class PreparedWikiChange:
    summary: str
    pages: tuple[str, ...]
    source_ids: tuple[str, ...]
    duplicate_pages: tuple[str, ...]
    conflict_pages: tuple[str, ...]
    transaction: WikiTransaction = field(repr=False)
    candidate_json: str = field(repr=False)
    context: WikiContext = field(repr=False)
    page_bases: tuple[_PreparedPageBase, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class CommitResult:
    global_revision: int
    pages: tuple[str, ...]
    search_index_current: bool


@dataclass(frozen=True, slots=True)
class WikiPageSummary:
    """One content page as a person browsing the Wiki sees it.

    A page that does not parse is still listed, with ``error`` saying why and
    no metadata, so it can be found and repaired rather than silently skipped.
    """

    logical_path: str
    title: str | None
    tags: tuple[str, ...]
    created: datetime | None
    updated: datetime | None
    revision: int | None
    source_count: int
    error: str | None


@dataclass(frozen=True, slots=True)
class WikiLogEntry:
    timestamp: str
    operation: str
    revision: int | None
    pages: tuple[str, ...]
    summary: str


@dataclass(frozen=True, slots=True)
class WikiPageListing:
    global_revision: int
    pages: tuple[WikiPageSummary, ...]


@dataclass(frozen=True, slots=True)
class _PreparedPageBase:
    logical_path: str
    content_hash: str | None


@dataclass(frozen=True, slots=True)
class _AuthoritySnapshot:
    global_revision: int
    raw_pages: Mapping[str, str]
    pages: tuple[WikiPage, ...]
    log_text: str
    needs_reindex: bool


class WikiManager:
    """Own initialization, safe reads, admission, transactions, cache, and lint."""

    def __init__(self, root: Path | None = None, *, wal: bool = True) -> None:
        self._close_lock = Lock()
        self._closed = False
        self.layout = ensure_wiki(root)
        self.registry = WorkspaceRegistry(self.layout.metadata / "workspaces.json")
        # Exposed for lock-state diagnostics and Task 10 approval-boundary tests.
        # Commit deliberately delegates locking to WikiTransaction.
        self.lock = WikiLock(self.layout.metadata / "locks" / "writer.lock")
        self.search_index = WikiSearchIndex.open(self.layout.database, wal=wal)
        try:
            self._search_index_current = self._refresh_search_index(force=True)
        except Exception:
            self._search_index_current = False

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self.search_index.close()
            self._closed = True

    def ensure(self) -> WikiLayout:
        return self.layout

    def search(self, query: str, limit: int) -> list[SearchResult]:
        snapshot = self._snapshot()
        try:
            current = self._sync_search_snapshot(
                snapshot,
                force=not self._search_index_current,
            )
        except Exception:
            current = False
        self._search_index_current = current
        if not current:
            return bounded_markdown_search(snapshot.pages, query, limit)
        try:
            return self.search_index.search(query, limit)
        except Exception:
            return bounded_markdown_search(snapshot.pages, query, limit)

    def read(self, page: str) -> WikiReadResult:
        logical_path = validate_logical_page(page).as_posix()
        with wiki_read_lock(self.layout, timeout=_LOCK_TIMEOUT_SECONDS):
            revision = _read_global_revision(self.layout)
            unresolved = self.layout.root / logical_path
            target = resolve_page_path(self.layout.root, logical_path)
            if unresolved.is_symlink() or not target.is_file():
                raise FileNotFoundError(logical_path)
            text = target.read_text(encoding="utf-8")
            parsed = parse_page(text, logical_path)
        return WikiReadResult(page=parsed, content=text, global_revision=revision)

    def prepare(
        self,
        candidate: WikiCandidate,
        context: WikiContext,
    ) -> PreparedWikiChange | DiscardedCandidate:
        """Prepare a complete mutation and release all locks before permission."""
        snapshot = self._snapshot()
        return self._prepare_from_snapshot(candidate, context, snapshot)

    def _prepare_from_snapshot(
        self,
        candidate: WikiCandidate,
        context: WikiContext,
        snapshot: _AuthoritySnapshot,
    ) -> PreparedWikiChange | DiscardedCandidate:
        decision = evaluate_candidate(candidate, context, snapshot.pages)
        if not decision.accepted:
            assert decision.reason is not None
            return DiscardedCandidate(
                reason=decision.reason,
                summary=candidate.summary,
                detail=decision.detail,
                retryable=decision.retryable,
            )
        if not self._workspace_sources_resolve(candidate):
            return DiscardedCandidate(reason="ungrounded", summary=candidate.summary)

        duplicate_pages = duplicate_candidate_paths(candidate, snapshot.pages)
        changes = _materialize_changes(
            candidate,
            context,
            snapshot.pages,
            excluded_paths=frozenset(duplicate_pages),
        )
        future_pages = {page.logical_path: page for page in snapshot.pages}
        for change in changes:
            future_pages[change.page.logical_path] = change.page
        index_bytes = _render_index(tuple(future_pages.values())).encode("utf-8")
        new_revision = snapshot.global_revision + 1
        log_bytes = _append_log(
            snapshot.log_text,
            operation=context.operation,
            summary=candidate.summary,
            pages=[change.page.logical_path for change in changes],
            source_ids=[_audit_source_id(source) for source in candidate.sources],
            revision=new_revision,
        ).encode("utf-8")
        transaction = WikiTransaction.prepare(
            layout=self.layout,
            changes=changes,
            expected_global_revision=snapshot.global_revision,
            index_bytes=index_bytes,
            log_bytes=log_bytes,
        )
        return PreparedWikiChange(
            summary=" ".join(candidate.summary.split())[:240],
            pages=tuple(change.page.logical_path for change in changes),
            source_ids=tuple(sorted(_audit_source_id(source) for source in candidate.sources)),
            duplicate_pages=duplicate_pages,
            conflict_pages=tuple(
                path for path in context.conflicting_pages if path in future_pages
            ),
            transaction=transaction,
            candidate_json=candidate.model_dump_json(),
            context=context,
            page_bases=_capture_page_bases(changes, snapshot.pages),
        )

    def commit(self, prepared: PreparedWikiChange) -> CommitResult:
        """Commit an already-decided proposal; WikiTransaction owns the writer lock."""
        current = prepared
        last_conflict: WikiConflictError | None = None
        for _attempt in range(4):
            try:
                revision = current.transaction.commit()
                break
            except WikiConflictError as exc:
                last_conflict = exc
                snapshot = self._snapshot()
                _assert_page_bases_unchanged(prepared.page_bases, snapshot.pages)
                try:
                    rebased = self._prepare_from_snapshot(
                        WikiCandidate.model_validate_json(prepared.candidate_json),
                        prepared.context,
                        snapshot,
                    )
                except WikiConflictError as rebase_conflict:
                    last_conflict = rebase_conflict
                    current = prepared
                    continue
                if isinstance(rebased, DiscardedCandidate):
                    raise WikiConflictError(
                        f"Wiki proposal is no longer admissible: {rebased.reason}"
                    ) from exc
                if rebased.pages != prepared.pages:
                    raise WikiConflictError(
                        "Wiki proposal changed shape during concurrent revalidation"
                    ) from exc
                current = rebased
        else:
            assert last_conflict is not None
            raise last_conflict
        try:
            search_current = self._refresh_search_index(force=True)
        except Exception:
            search_current = False
        self._search_index_current = search_current
        return CommitResult(
            global_revision=revision,
            pages=current.pages,
            search_index_current=search_current,
        )

    def ingest(
        self,
        source: CurrentSource,
        instructions: WikiCandidate,
        context: WikiContext,
    ) -> PreparedWikiChange | DiscardedCandidate:
        """Validate a current-session source, then prepare its structured candidate.

        The caller/model supplies the structured candidate as ``instructions``;
        this manager never performs a hidden model call or stores raw source text.
        """
        expected_source = self._source_ref_for_ingest(source, context)
        if isinstance(expected_source, DiscardedCandidate):
            return expected_source
        # The ingest source is provenance the manager derived, so it attaches it
        # rather than demanding the candidate repeat a hash back. Asking for the
        # hash only ever produced a mismatch the model could not correct.
        instructions = _with_source(instructions, expected_source)
        ingest_context = context.model_copy(update={"operation": "ingest"})
        return self.prepare(instructions, ingest_context)

    def list_pages(self) -> WikiPageListing:
        snapshot = self._snapshot()
        summaries: list[WikiPageSummary] = []
        for logical_path, text in sorted(snapshot.raw_pages.items()):
            try:
                page = parse_page(text, logical_path)
            except (ValueError, UnicodeError) as exc:
                summaries.append(
                    WikiPageSummary(
                        logical_path=logical_path,
                        title=None,
                        tags=(),
                        created=None,
                        updated=None,
                        revision=None,
                        source_count=0,
                        error=str(exc) or type(exc).__name__,
                    )
                )
                continue
            summaries.append(
                WikiPageSummary(
                    logical_path=logical_path,
                    title=page.title,
                    tags=tuple(page.tags),
                    created=page.created,
                    updated=page.updated,
                    revision=page.revision,
                    source_count=len(page.sources),
                    error=None,
                )
            )
        return WikiPageListing(global_revision=snapshot.global_revision, pages=tuple(summaries))

    def read_log(self) -> str:
        with wiki_read_lock(self.layout, timeout=_LOCK_TIMEOUT_SECONDS):
            return self.layout.log.read_text(encoding="utf-8")

    def save_page(
        self,
        *,
        logical_path: str,
        title: str,
        tags: list[str],
        body: str,
        expected_revision: int | None,
        summary: str | None = None,
    ) -> CommitResult:
        """Write one page as a person edited it, bypassing the model's admission gate.

        The gate decides whether a model's conclusion is worth keeping; a person
        writing the page has already decided that. Everything that keeps a page
        storable still applies — the schema, the body rules in `render_page`,
        and the credential check — and so does the revision the editor started
        from: an edit made against a page that has moved on is refused, not merged.
        """
        logical_path = validate_logical_page(logical_path).as_posix()
        if contains_sensitive_text(title) or contains_sensitive_text(body):
            raise ValueError("Wiki pages cannot contain credentials or secrets")
        snapshot = self._snapshot()
        existing = {page.logical_path: page for page in snapshot.pages}
        now = datetime.now().astimezone()
        if expected_revision is None:
            if logical_path in snapshot.raw_pages:
                raise WikiConflictError(f"page already exists: {logical_path}")
            page = WikiPage(
                logical_path=logical_path,
                title=title,
                created=now,
                updated=now,
                tags=tags,
                revision=1,
                body=body,
            )
            operation = "admin-create"
        else:
            current = existing.get(logical_path)
            if current is None:
                raise WikiConflictError(f"page is missing: {logical_path}")
            if current.revision != expected_revision:
                raise WikiConflictError(f"page revision changed: {logical_path}")
            page = WikiPage(
                logical_path=logical_path,
                title=title,
                created=current.created,
                updated=max(now, current.updated),
                tags=tags,
                sources=current.sources,
                revision=current.revision + 1,
                body=body,
            )
            operation = "admin-edit"
        # Rendering applies the body rules before anything is prepared, so a
        # refused page surfaces as a validation error rather than a conflict.
        render_page(page)
        existing[logical_path] = page
        return self._commit_admin(
            snapshot,
            changes=(PageChange(page=page, expected_revision=expected_revision),),
            deletions=(),
            future_pages=tuple(existing.values()),
            operation=operation,
            summary=summary or f"{operation} {logical_path}",
        )

    def delete_page(
        self,
        logical_path: str,
        *,
        expected_revision: int,
        summary: str | None = None,
    ) -> CommitResult:
        logical_path = validate_logical_page(logical_path).as_posix()
        deletion = PageDeletion(logical_path=logical_path, expected_revision=expected_revision)
        snapshot = self._snapshot()
        existing = {page.logical_path: page for page in snapshot.pages}
        current = existing.pop(deletion.logical_path, None)
        if current is None:
            raise WikiConflictError(f"page is missing: {deletion.logical_path}")
        if current.revision != expected_revision:
            raise WikiConflictError(f"page revision changed: {deletion.logical_path}")
        return self._commit_admin(
            snapshot,
            changes=(),
            deletions=(deletion,),
            future_pages=tuple(existing.values()),
            operation="admin-delete",
            summary=summary or f"admin-delete {deletion.logical_path}",
        )

    def _commit_admin(
        self,
        snapshot: _AuthoritySnapshot,
        *,
        changes: tuple[PageChange, ...],
        deletions: tuple[PageDeletion, ...],
        future_pages: tuple[WikiPage, ...],
        operation: str,
        summary: str,
    ) -> CommitResult:
        new_revision = snapshot.global_revision + 1
        paths = sorted(
            [change.page.logical_path for change in changes]
            + [deletion.logical_path for deletion in deletions]
        )
        transaction = WikiTransaction.prepare(
            layout=self.layout,
            changes=changes,
            deletions=deletions,
            expected_global_revision=snapshot.global_revision,
            index_bytes=_render_index(future_pages).encode("utf-8"),
            log_bytes=_append_log(
                snapshot.log_text,
                operation=operation,
                summary=summary,
                pages=paths,
                source_ids=(),
                revision=new_revision,
            ).encode("utf-8"),
        )
        # No rebase on conflict, unlike a model's proposal: the person editing
        # saw one version of the page, and only they can say what to do with another.
        revision = transaction.commit()
        try:
            search_current = self._refresh_search_index(force=True)
        except Exception:
            search_current = False
        self._search_index_current = search_current
        return CommitResult(
            global_revision=revision,
            pages=tuple(paths),
            search_index_current=search_current,
        )

    def lint(self, scope: str | None) -> LintReport:
        if scope is not None and scope not in CATEGORY_DIRS:
            raise ValueError("Wiki lint scope must be a declared category")
        snapshot = self._snapshot()
        return lint_snapshot(
            snapshot.raw_pages,
            scope=scope,
            resolve_source=self.registry.resolve,
            hand_written=hand_written_pages(parse_log(snapshot.log_text)),
        )

    def _source_ref_for_ingest(
        self,
        source: CurrentSource,
        context: WikiContext,
    ) -> SourceRef | DiscardedCandidate:
        if source.kind == "inline":
            assert source.content is not None
            if contains_sensitive_text(source.content):
                return DiscardedCandidate(reason="sensitive", summary="Ingest source rejected")
            return SourceRef(
                kind="conversation",
                session_id=context.session_id,
                content_hash=content_hash(source.content.encode("utf-8")),
            )
        assert source.workspace_id is not None and source.relative_path is not None
        portable = SourceRef(
            kind="workspace-file",
            workspace_id=source.workspace_id,
            path=source.relative_path,
            content_hash="sha256:" + "0" * 64,
        )
        resolved = self.registry.resolve(portable)
        if resolved is None:
            return DiscardedCandidate(reason="ungrounded", summary="Ingest source rejected")
        data = resolved.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return DiscardedCandidate(reason="sensitive", summary="Ingest source rejected")
        if contains_sensitive_text(text):
            return DiscardedCandidate(reason="sensitive", summary="Ingest source rejected")
        return portable.model_copy(update={"content_hash": content_hash(data)})

    def _workspace_sources_resolve(self, candidate: WikiCandidate) -> bool:
        """Every workspace source must still name a readable file in its workspace.

        Not the same bytes, though. The agent edits the files it reads, so a
        candidate that cited one was refused for the rest of the session — the
        runtime-attached provenance included, which nothing could correct. The
        stored `SourceRef` keeps the hash that was actually observed, and
        `wiki.lint` reports the divergence as `stale_provenance`; a page whose
        source has moved on is a page to flag, not a write to lose.
        """
        sources = (
            *candidate.sources,
            *(source for change in candidate.pages for source in change.page.sources),
        )
        checked: set[str] = set()
        for source in sources:
            if source.kind != "workspace-file":
                continue
            key = source.model_dump_json(exclude_none=True)
            if key in checked:
                continue
            checked.add(key)
            if self.registry.resolve(source) is None:
                return False
        return True

    def _snapshot(self) -> _AuthoritySnapshot:
        with wiki_read_lock(self.layout, timeout=_LOCK_TIMEOUT_SECONDS) as recovery:
            revision = _read_global_revision(self.layout)
            raw_pages = _read_raw_pages(self.layout)
            pages = _parse_valid_pages(raw_pages)
            log_text = self.layout.log.read_text(encoding="utf-8")
            needs_reindex = recovery.needs_reindex
        return _AuthoritySnapshot(
            global_revision=revision,
            raw_pages=raw_pages,
            pages=pages,
            log_text=log_text,
            needs_reindex=needs_reindex,
        )

    def _refresh_search_index(self, *, force: bool) -> bool:
        """Sync one full revision snapshot, then conditionally acknowledge that revision."""
        snapshot = self._snapshot()
        return self._sync_search_snapshot(snapshot, force=force)

    def _sync_search_snapshot(
        self,
        snapshot: _AuthoritySnapshot,
        *,
        force: bool,
    ) -> bool:
        if not force and not snapshot.needs_reindex:
            return True
        # Intentionally outside wiki_read_lock: SQLite callbacks and diagnostics
        # must be able to call manager.read without re-entering a shared lock.
        applied = self.search_index.sync(
            snapshot.pages,
            revision=snapshot.global_revision,
        )
        if not applied:
            return False
        acknowledgement = acknowledge_reindex(
            self.layout,
            rebuilt_revision=snapshot.global_revision,
            timeout=_LOCK_TIMEOUT_SECONDS,
        )
        return acknowledgement.acknowledged


def _read_global_revision(layout: WikiLayout) -> int:
    raw = layout.revision.read_text(encoding="ascii").strip()
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError("Wiki revision must be a non-negative integer")
    return int(raw)


def _read_raw_pages(layout: WikiLayout) -> dict[str, str]:
    pages: dict[str, str] = {}
    for category in CATEGORY_DIRS:
        directory = layout.root / category
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or not directory.resolve(strict=True).is_relative_to(layout.root.resolve(strict=True))
        ):
            continue
        for target in sorted(directory.rglob("*.md"), key=lambda path: path.as_posix()):
            relative_target = target.relative_to(directory).as_posix()
            logical_path = f"{category}/{relative_target}"
            try:
                safe_file = (
                    not target.is_symlink()
                    and target.is_file()
                    and target.resolve(strict=True).is_relative_to(layout.root.resolve(strict=True))
                )
                pages[logical_path] = target.read_text(encoding="utf-8") if safe_file else ""
            except (OSError, UnicodeError, ValueError):
                pages[logical_path] = ""
    return pages


def _parse_valid_pages(raw_pages: Mapping[str, str]) -> tuple[WikiPage, ...]:
    pages: list[WikiPage] = []
    for logical_path, text in sorted(raw_pages.items()):
        try:
            pages.append(parse_page(text, logical_path))
        except (ValueError, UnicodeError):
            continue
    return tuple(pages)


def _capture_page_bases(
    changes: tuple[PageChange, ...],
    existing_pages: tuple[WikiPage, ...],
) -> tuple[_PreparedPageBase, ...]:
    existing = {page.logical_path: page for page in existing_pages}
    return tuple(
        _PreparedPageBase(
            logical_path=change.page.logical_path,
            content_hash=(
                content_hash(render_page(existing[change.page.logical_path]).encode("utf-8"))
                if change.page.logical_path in existing
                else None
            ),
        )
        for change in changes
    )


def _assert_page_bases_unchanged(
    page_bases: tuple[_PreparedPageBase, ...],
    current_pages: tuple[WikiPage, ...],
) -> None:
    current = {
        page.logical_path: content_hash(render_page(page).encode("utf-8")) for page in current_pages
    }
    for base in page_bases:
        if current.get(base.logical_path) != base.content_hash:
            raise WikiConflictError(f"page changed after approval: {base.logical_path}")


def _with_source(candidate: WikiCandidate, source: SourceRef) -> WikiCandidate:
    """Attach one runtime-derived source to a candidate and each of its pages."""
    key = source.model_dump_json(exclude_none=True)

    def _extend(existing: list[SourceRef]) -> list[SourceRef]:
        if any(item.model_dump_json(exclude_none=True) == key for item in existing):
            return list(existing)
        return [*existing, source]

    return candidate.model_copy(
        update={
            "sources": _extend(candidate.sources),
            "pages": [
                change.model_copy(
                    update={
                        "page": change.page.model_copy(
                            update={"sources": _extend(change.page.sources)}
                        )
                    }
                )
                for change in candidate.pages
            ],
        }
    )


def _materialize_changes(
    candidate: WikiCandidate,
    context: WikiContext,
    existing_pages: tuple[WikiPage, ...],
    *,
    excluded_paths: frozenset[str] = frozenset(),
) -> tuple[PageChange, ...]:
    existing_by_path = {page.logical_path: page for page in existing_pages}
    conflicts = set(context.conflicting_pages)
    changes: list[PageChange] = []
    for proposed in candidate.pages:
        if proposed.page.logical_path in excluded_paths:
            continue
        current = existing_by_path.get(proposed.page.logical_path)
        if current is None:
            if proposed.expected_revision is not None:
                raise WikiRevisionMismatch(
                    proposed.page.logical_path, proposed.expected_revision, None
                )
            page = proposed.page.model_copy(update={"revision": 1})
            changes.append(PageChange(page=page, expected_revision=None))
            continue
        if proposed.expected_revision != current.revision:
            raise WikiRevisionMismatch(
                proposed.page.logical_path, proposed.expected_revision, current.revision
            )
        sources = _merge_sources(current.sources, proposed.page.sources)
        body = (
            _merge_conflict(
                current.body,
                proposed.page.body,
                current.sources,
                proposed.page.sources,
            )
            if proposed.page.logical_path in conflicts
            else proposed.page.body
        )
        updated = max(proposed.page.updated, datetime.now().astimezone(), current.updated)
        page = proposed.page.model_copy(
            update={
                "created": current.created,
                "updated": updated,
                "sources": sources,
                "revision": current.revision + 1,
                "body": body,
            }
        )
        changes.append(PageChange(page=page, expected_revision=current.revision))
    return tuple(changes)


def _merge_sources(existing: list[SourceRef], proposed: list[SourceRef]) -> list[SourceRef]:
    result: list[SourceRef] = []
    seen: set[str] = set()
    for source in (*existing, *proposed):
        key = source.model_dump_json(exclude_none=True)
        if key not in seen:
            seen.add(key)
            result.append(source)
    return result


def _merge_conflict(
    existing: str,
    proposed: str,
    existing_sources: list[SourceRef],
    proposed_sources: list[SourceRef],
) -> str:
    existing_ids = ", ".join(sorted(_audit_source_id(source) for source in existing_sources))
    proposed_ids = ", ".join(sorted(_audit_source_id(source) for source in proposed_sources))
    return (
        "## Conflict\n\n"
        "### Existing sourced position\n\n"
        f"Sources: {existing_ids}\n\n"
        f"{existing.strip()}\n\n"
        "### Additional sourced position\n\n"
        f"Sources: {proposed_ids}\n\n"
        f"{proposed.strip()}\n"
    )


def _render_index(pages: tuple[WikiPage, ...]) -> str:
    by_category: dict[str, list[WikiPage]] = {category: [] for category in CATEGORY_DIRS}
    for page in pages:
        by_category[page.logical_path.split("/", 1)[0]].append(page)
    lines = ["# Wiki Index", ""]
    for category in CATEGORY_DIRS:
        lines.extend((f"## {_CATEGORY_LABELS[category]}", ""))
        for page in sorted(by_category[category], key=lambda item: item.logical_path):
            link = page.logical_path.removesuffix(".md")
            lines.append(f"- [[{link}]] — {_one_line_summary(page.body)}")
        if by_category[category]:
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _one_line_summary(body: str) -> str:
    for line in body.splitlines():
        normalized = line.strip().lstrip("-* ").strip()
        if normalized and not normalized.startswith("#"):
            return normalized[:240]
    return "No summary available."


def _append_log(
    previous: str,
    *,
    operation: str,
    summary: str,
    pages: Sequence[str],
    source_ids: Sequence[str],
    revision: int,
) -> str:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    joined_pages = ",".join(pages)
    sources = ",".join(sorted(source_ids))
    summary = " ".join(summary.split()).replace("%", "%25").replace("|", "%7C")
    entry = (
        f"{timestamp} | operation={operation} | revision={revision} | "
        f"pages={joined_pages} | sources={sources} | summary={summary}\n"
    )
    return previous.rstrip() + "\n\n" + entry


_LOG_ENTRY = re.compile(r"^(\S+) \| operation=([^|]*) \| (.*)$")
_ADMIN_WRITES = frozenset({"admin-create", "admin-edit"})


def parse_log(text: str) -> list[WikiLogEntry]:
    """The audit entries `_append_log` wrote, oldest first; other lines are skipped."""
    entries: list[WikiLogEntry] = []
    for line in text.splitlines():
        match = _LOG_ENTRY.match(line.strip())
        if match is None:
            continue
        fields: dict[str, str] = {}
        for part in match.group(3).split(" | "):
            key, _, value = part.partition("=")
            fields[key] = value
        revision = fields.get("revision", "")
        entries.append(
            WikiLogEntry(
                timestamp=match.group(1),
                operation=match.group(2).strip(),
                revision=int(revision) if revision.isdecimal() else None,
                pages=tuple(page for page in fields.get("pages", "").split(",") if page),
                summary=fields.get("summary", "").replace("%7C", "|").replace("%25", "%"),
            )
        )
    return entries


def hand_written_pages(entries: Sequence[WikiLogEntry]) -> frozenset[str]:
    """Pages whose latest committed write came from a person in the admin panel.

    The log is committed in the same transaction as the pages, so it says
    reliably who wrote a page last. A model's write always carries sources;
    a page without any that the log does not attribute to a person was
    changed outside the Wiki, and is still worth reporting.
    """
    pages: set[str] = set()
    for entry in entries:
        for path in entry.pages:
            if entry.operation in _ADMIN_WRITES:
                pages.add(path)
            else:
                pages.discard(path)
    return frozenset(pages)


def _audit_source_id(source: SourceRef) -> str:
    if source.kind == "conversation":
        base = f"conversation:{source.session_id}"
    elif source.kind == "workspace-file":
        base = f"workspace:{source.workspace_id}"
    else:
        base = "web"
    return f"{base}@{source.content_hash}"
