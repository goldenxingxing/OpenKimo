# Global User Wiki Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one installation-ready, local-first Markdown Wiki shared by every workspace of the current OpenKimo user, with bounded session awareness, safe search/read/write tools, approval-aware writes, and crash recovery.

**Architecture:** Markdown under `<OpenKimo application data>/users/default/wiki` is the only authority; `WikiManager` provides initialization, validation, workspace provenance, locking, transactional writes, recovery, and a disposable SQLite FTS5 cache. Every session resolves the same manager, receives a bounded compact index, and exposes one controlled Wiki tool; ordinary file tools explicitly reject the managed root.

**Tech Stack:** Python 3.12+, Pydantic 2, PyYAML, stdlib `sqlite3`/FTS5/WAL, `fcntl`/`msvcrt` file locks, FastAPI, existing Kimi `CallableTool2` and Approval wire/UI, pytest/pytest-asyncio, React/TypeScript/Vite, macOS `.app`, Windows Inno Setup.

## Global Constraints

- Markdown is the only authoritative store; SQLite is disposable and rebuildable.
- Use exactly one current namespace: `users/default/wiki`; this is only a future multi-user migration seam.
- Do not copy, scan, package, import, or migrate `/Users/qunwei/Documents/local_agent_work/wiki` or any private page.
- Package only generic `schema.md`, `index.md`, `overview.md`, and `log.md` templates plus empty category directories.
- Normalize the category name to `comparisons`; never introduce `comparations`.
- Do not add a standalone runtime, service, daemon, watcher, vector embeddings, vector database, or model reranker.
- Do not change the Admin Panel or add a Wiki tab.
- Do not expose directory/ZIP import; `ingest` accepts only current-session user-provided content or a permitted workspace file.
- Do not add session-end extraction, summarization, model calls, candidate queues, or automatic archival.
- Normal-mode writes use existing Approval; YOLO may write proactively only after the same value/safety gate.
- Approval is resolved before acquiring the writer lock.
- Ordinary filesystem tools must not mutate the managed Wiki.
- Never store credentials, cookies, environment secrets, machine-specific absolute paths, or full conversation transcripts in Wiki pages or `log.md`.
- Docker/KAOS deployments must explicitly configure one shared Markdown root; shared-filesystem SQLite may be process-local or disabled.
- Do not modify, stage, or commit `packaging/venvstacks.resolved.toml`.

## File Structure

- `kimi-cli/src/kimi_cli/wiki/models.py`: page/source/change/search/result models and typed errors.
- `kimi-cli/src/kimi_cli/wiki/paths.py`: platform/config root resolution and logical-path containment.
- `kimi-cli/src/kimi_cli/wiki/templates/`: generic packaged Markdown and manifest templates.
- `kimi-cli/src/kimi_cli/wiki/schema.py`: frontmatter parsing, validation, hashes, links, revisions.
- `kimi-cli/src/kimi_cli/wiki/workspaces.py`: stable UUID registry and portable source resolution.
- `kimi-cli/src/kimi_cli/wiki/search.py`: FTS5/trigram/WAL cache, fallback search, rebuild.
- `kimi-cli/src/kimi_cli/wiki/locking.py`: cross-process shared/exclusive lock.
- `kimi-cli/src/kimi_cli/wiki/transaction.py`: durable journal, atomic replacement, recovery.
- `kimi-cli/src/kimi_cli/wiki/manager.py`: single application service boundary.
- `kimi-cli/src/kimi_cli/wiki/context.py`: compact UTF-8 index rendering and short guidance.
- `kimi-cli/src/kimi_cli/tools/wiki.py`: controlled model-facing `Wiki` tool.
- `kimi-cli/src/kimi_cli/soul/agent.py`, `kimi-cli/src/kimi_cli/agents/default/{agent.yaml,system.md}`: runtime wiring and prompt injection.
- `kimi-cli/src/kimi_cli/tools/file/{write.py,replace.py}`: managed-root mutation guard.
- `kimi-cli/src/kimi_cli/web/api/memory.py`: move legacy Knowledge routes off session-derived scope and enforce ownership for legacy session resources.
- `packaging/app_main/{paths.py,server.py}`, `packaging/app_main_win/paths.py`: desktop path and environment wiring.
- Tests live under matching `kimi-cli/tests/wiki/`, `kimi-cli/tests/tools/`, `kimi-cli/tests/core/`, `kimi-cli/tests/web/`, and root `tests/`.

---

### Task 1: Generic Packaged Wiki Skeleton and User-Level Path

**Files:**
- Create: `kimi-cli/src/kimi_cli/wiki/__init__.py`
- Create: `kimi-cli/src/kimi_cli/wiki/paths.py`
- Create: `kimi-cli/src/kimi_cli/wiki/templates/schema.md`
- Create: `kimi-cli/src/kimi_cli/wiki/templates/index.md`
- Create: `kimi-cli/src/kimi_cli/wiki/templates/overview.md`
- Create: `kimi-cli/src/kimi_cli/wiki/templates/log.md`
- Create: `kimi-cli/src/kimi_cli/wiki/templates/manifest.json`
- Create: `kimi-cli/tests/wiki/test_paths.py`
- Create: `kimi-cli/tests/wiki/test_initialization.py`
- Modify: `packaging/app_main/paths.py`
- Modify: `packaging/app_main_win/paths.py`
- Modify: `packaging/app_main/server.py`
- Modify: `tests/test_work_directory_settings.py`
- Modify: `tests/test_skill_packaging.py`

**Interfaces:**
- Consumes: existing `AppPaths.app_support: Path` and desktop `_build_env(AppPaths) -> dict[str, str]`.
- Produces: `resolve_wiki_root(*, app_data: Path | None = None) -> Path`, `WIKI_SCHEMA_VERSION = 1`, and environment key `OPENKIMO_WIKI_ROOT`.

- [ ] **Step 1: Write failing path, package, and privacy tests**

```python
def test_resolve_wiki_root_uses_default_user_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENKIMO_APP_DATA_DIR", str(tmp_path))
    from kimi_cli.wiki.paths import resolve_wiki_root
    assert resolve_wiki_root() == tmp_path / "users" / "default" / "wiki"

def test_packaged_templates_are_generic():
    root = Path("kimi-cli/src/kimi_cli/wiki/templates")
    text = "\n".join(p.read_text() for p in root.glob("*"))
    assert "comparisons" in text
    for forbidden in ("local_agent_work", "/Users/qunwei", "Obsidian", "TaskOutput"):
        assert forbidden not in text
```

- [ ] **Step 2: Run tests and verify the red state**

Run: `kimi-cli/.venv/bin/python -m pytest kimi-cli/tests/wiki/test_paths.py kimi-cli/tests/wiki/test_initialization.py tests/test_work_directory_settings.py tests/test_skill_packaging.py -q`

Expected: FAIL because `kimi_cli.wiki.paths` and template assets do not exist and `AppPaths` has no `wiki_dir`.

- [ ] **Step 3: Add path resolver, desktop field, environment, and templates**

```python
# kimi-cli/src/kimi_cli/wiki/paths.py
WIKI_SCHEMA_VERSION = 1

def resolve_wiki_root(*, app_data: Path | None = None) -> Path:
    configured = os.environ.get("OPENKIMO_WIKI_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    base = app_data or Path(os.environ["OPENKIMO_APP_DATA_DIR"])
    return (base / "users" / "default" / "wiki").resolve()
```

Add `wiki_dir: Path` to both `AppPaths` dataclasses, assign `app_support / "users" / "default" / "wiki"`, create it in `ensure_dirs()`, and add:

```python
env["OPENKIMO_APP_DATA_DIR"] = str(p.app_support)
env["OPENKIMO_WIKI_ROOT"] = str(p.wiki_dir)
```

`manifest.json` must be exactly `{"schema_version":1,"namespace":"default"}`. The four Markdown templates must contain only generic schema/catalog/audit headings; category directories are created at initialization, not represented with private sample pages.

- [ ] **Step 4: Run tests and verify green**

Run: `kimi-cli/.venv/bin/python -m pytest kimi-cli/tests/wiki/test_paths.py kimi-cli/tests/wiki/test_initialization.py tests/test_work_directory_settings.py tests/test_skill_packaging.py -q`

Expected: PASS; package inspection finds no private Wiki content or private absolute path.

- [ ] **Step 5: Commit**

```bash
git add kimi-cli/src/kimi_cli/wiki kimi-cli/tests/wiki/test_paths.py kimi-cli/tests/wiki/test_initialization.py packaging/app_main/paths.py packaging/app_main_win/paths.py packaging/app_main/server.py tests/test_work_directory_settings.py tests/test_skill_packaging.py
git commit -m "feat: package global wiki skeleton"
```

### Task 2: Page Schema, Logical Paths, and Safety

**Files:**
- Create: `kimi-cli/src/kimi_cli/wiki/models.py`
- Create: `kimi-cli/src/kimi_cli/wiki/schema.py`
- Create: `kimi-cli/tests/wiki/test_schema.py`
- Create: `kimi-cli/tests/wiki/test_path_safety.py`

**Interfaces:**
- Consumes: `resolve_wiki_root()` and `WIKI_SCHEMA_VERSION`.
- Produces: `SourceRef`, `CurrentSource`, `WikiCandidate`, `WikiPage`, `PageChange`, `validate_logical_page(page: str) -> PurePosixPath`, `parse_page(text: str, logical_path: str) -> WikiPage`, `render_page(page: WikiPage) -> str`, `content_hash(data: bytes) -> str`.

- [ ] **Step 1: Write failing schema and escape tests**

```python
@pytest.mark.parametrize("bad", ["../secret.md", "/tmp/x.md", "entities/../../x.md", ".openkimo/revision"])
def test_logical_page_rejects_escape(bad):
    with pytest.raises(UnsafeWikiPath):
        validate_logical_page(bad)

def test_page_round_trip_increments_revision():
    page = parse_page(VALID_PAGE, "concepts/atomic-writes.md")
    updated = page.model_copy(update={"revision": page.revision + 1})
    assert parse_page(render_page(updated), "concepts/atomic-writes.md").revision == 2
```

- [ ] **Step 2: Run tests and verify the red state**

Run: `cd kimi-cli && uv run pytest tests/wiki/test_schema.py tests/wiki/test_path_safety.py -q`

Expected: FAIL because schema models and validators are absent.

- [ ] **Step 3: Implement strict models and canonical logical paths**

```python
class SourceRef(BaseModel):
    kind: Literal["workspace-file", "conversation", "web"]
    workspace_id: UUID | None = None
    path: str | None = None
    session_id: UUID | None = None
    url: HttpUrl | None = None
    content_hash: str

class CurrentSource(BaseModel):
    kind: Literal["inline", "workspace-file"]
    content: str | None = None
    workspace_id: UUID | None = None
    relative_path: str | None = None

class WikiCandidate(BaseModel):
    summary: str
    pages: list[PageChange]
    sources: list[SourceRef]
    value: Literal["high", "medium", "low"]

class WikiPage(BaseModel):
    logical_path: str
    title: str
    created: datetime
    updated: datetime
    tags: list[str]
    sources: list[SourceRef]
    revision: PositiveInt
    body: str

def validate_logical_page(page: str) -> PurePosixPath:
    p = PurePosixPath(page)
    if p.is_absolute() or ".." in p.parts or p.suffix != ".md":
        raise UnsafeWikiPath(page)
    if p.parts[0] not in {"entities", "concepts", "comparisons", "sources", "queries", "lint"}:
        raise UnsafeWikiPath(page)
    if any(not SLUG_RE.fullmatch(part.removesuffix(".md")) for part in p.parts):
        raise UnsafeWikiPath(page)
    return p
```

Parse YAML with `yaml.safe_load`, require all declared fields, reject unknown source kinds, secrets matching existing `kimi_cli.utils.sensitive` patterns, absolute provenance paths, malformed WikiLinks, and revisions below 1. Resolve final filesystem targets with `Path.resolve(strict=False)` plus `is_relative_to(root.resolve())`; reject symlink escape before read or write.

- [ ] **Step 4: Run tests and verify green**

Run: `cd kimi-cli && uv run pytest tests/wiki/test_schema.py tests/wiki/test_path_safety.py -q`

Expected: PASS for valid English/Chinese pages; traversal, absolute paths, symlink escape, credentials, and malformed frontmatter are rejected.

- [ ] **Step 5: Commit**

```bash
git add kimi-cli/src/kimi_cli/wiki/models.py kimi-cli/src/kimi_cli/wiki/schema.py kimi-cli/tests/wiki/test_schema.py kimi-cli/tests/wiki/test_path_safety.py
git commit -m "feat: validate global wiki pages"
```

### Task 3: Idempotent Initialization and Versioned Metadata

**Files:**
- Create: `kimi-cli/src/kimi_cli/wiki/initialize.py`
- Modify: `kimi-cli/src/kimi_cli/wiki/__init__.py`
- Modify: `kimi-cli/tests/wiki/test_initialization.py`

**Interfaces:**
- Consumes: packaged templates and `resolve_wiki_root()`.
- Produces: `WikiLayout`, `ensure_wiki(root: Path | None = None) -> WikiLayout`, `UnsupportedWikiSchema`.

- [ ] **Step 1: Add failing idempotency and preservation tests**

```python
def test_ensure_wiki_is_idempotent_and_preserves_markdown(tmp_path):
    layout = ensure_wiki(tmp_path / "wiki")
    layout.index.write_text("# My edited index\n", encoding="utf-8")
    second = ensure_wiki(layout.root)
    assert second.index.read_text(encoding="utf-8") == "# My edited index\n"
    assert {p.name for p in layout.root.iterdir()} >= {
        "schema.md", "index.md", "overview.md", "log.md",
        "entities", "concepts", "comparisons", "sources", "queries", "lint", ".openkimo",
    }
```

- [ ] **Step 2: Run test and verify red**

Run: `cd kimi-cli && uv run pytest tests/wiki/test_initialization.py -q`

Expected: FAIL because `ensure_wiki` is undefined.

- [ ] **Step 3: Implement create-if-missing initialization**

```python
@dataclass(frozen=True, slots=True)
class WikiLayout:
    root: Path
    index: Path
    overview: Path
    log: Path
    metadata: Path
    revision: Path
    database: Path

def ensure_wiki(root: Path | None = None) -> WikiLayout:
    layout = layout_for(root or resolve_wiki_root())
    for name in CATEGORY_DIRS:
        (layout.root / name).mkdir(parents=True, exist_ok=True)
    for name in ("journal", "locks"):
        (layout.metadata / name).mkdir(parents=True, exist_ok=True)
    for name in SPECIAL_FILES:
        _copy_template_exclusive(name, layout.root / name)
    manifest = _read_manifest(layout.metadata / "manifest.json")
    if manifest["schema_version"] > WIKI_SCHEMA_VERSION:
        raise UnsupportedWikiSchema(manifest["schema_version"])
    layout.revision.touch(exist_ok=True)
    if not layout.revision.read_text(encoding="ascii").strip():
        layout.revision.write_text("0\n", encoding="ascii")
    return layout
```

Use exclusive creation (`open("x")`) so upgrades never overwrite user Markdown. Permit only explicit metadata migration functions keyed by integer schema version.

- [ ] **Step 4: Run tests and verify green**

Run: `cd kimi-cli && uv run pytest tests/wiki/test_initialization.py -q`

Expected: PASS; repeated initialization preserves edited special files and rejects future schema versions.

- [ ] **Step 5: Commit**

```bash
git add kimi-cli/src/kimi_cli/wiki/__init__.py kimi-cli/src/kimi_cli/wiki/initialize.py kimi-cli/tests/wiki/test_initialization.py
git commit -m "feat: initialize global wiki idempotently"
```

### Task 4: Stable Workspace Registry and Portable Provenance

**Files:**
- Create: `kimi-cli/src/kimi_cli/wiki/workspaces.py`
- Create: `kimi-cli/tests/wiki/test_workspaces.py`
- Modify: `kimi-cli/src/kimi_cli/wiki/models.py`

**Interfaces:**
- Consumes: `WikiLayout.metadata`.
- Produces: `WorkspaceRegistry.register(path: Path) -> UUID`, `WorkspaceRegistry.relative_source(workspace_id: UUID, path: Path) -> SourceRef`, `WorkspaceRegistry.resolve(source: SourceRef) -> Path | None`.

- [ ] **Step 1: Write failing register/move/escape tests**

```python
def test_workspace_move_updates_only_registry(tmp_path):
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    old = tmp_path / "old"; old.mkdir()
    workspace_id = registry.register(old)
    moved = tmp_path / "moved"; old.rename(moved)
    assert registry.register(moved, workspace_id=workspace_id) == workspace_id
    assert json.loads(registry.path.read_text())["workspaces"][str(workspace_id)]["path"] == str(moved)

def test_unknown_and_escape_sources_are_not_executable(registry, workspace_id):
    source = SourceRef(
        kind="workspace-file",
        workspace_id=workspace_id,
        path="../secret",
        content_hash=HASH,
    )
    assert registry.resolve(source) is None
```

- [ ] **Step 2: Run tests and verify red**

Run: `cd kimi-cli && uv run pytest tests/wiki/test_workspaces.py -q`

Expected: FAIL because `WorkspaceRegistry` does not exist.

- [ ] **Step 3: Implement atomic registry updates and contained source resolution**

```python
class WorkspaceRegistry:
    def register(self, path: Path, *, workspace_id: UUID | None = None) -> UUID:
        canonical = path.resolve(strict=True)
        data = self._read()
        existing = self._id_for_path(data, canonical)
        key = existing or workspace_id or uuid4()
        data["workspaces"][str(key)] = {
            "path": str(canonical),
            "last_seen_at": datetime.now().astimezone().isoformat(),
        }
        atomic_json_replace(self.path, data)
        return key

    def resolve(self, source: SourceRef) -> Path | None:
        root = self._registered_root(source.workspace_id)
        if root is None or source.path is None or Path(source.path).is_absolute():
            return None
        candidate = (root / source.path).resolve(strict=False)
        return candidate if candidate.is_relative_to(root.resolve()) else None
```

Guard registry writes with its own cross-process lock, store absolute paths only in `.openkimo/workspaces.json`, and ensure page provenance stores UUID plus POSIX relative path only.

- [ ] **Step 4: Run tests and verify green**

Run: `cd kimi-cli && uv run pytest tests/wiki/test_workspaces.py -q`

Expected: PASS; moves preserve UUID and do not rewrite Wiki pages; unknown/missing/escaping sources resolve to `None`.

- [ ] **Step 5: Commit**

```bash
git add kimi-cli/src/kimi_cli/wiki/models.py kimi-cli/src/kimi_cli/wiki/workspaces.py kimi-cli/tests/wiki/test_workspaces.py
git commit -m "feat: add portable wiki provenance"
```

### Task 5: Disposable FTS5 Search Cache and Markdown Fallback

**Files:**
- Create: `kimi-cli/src/kimi_cli/wiki/search.py`
- Create: `kimi-cli/tests/wiki/test_search.py`
- Create: `kimi-cli/tests/wiki/test_search_recovery.py`

**Interfaces:**
- Consumes: validated `WikiPage` and Markdown content hashes.
- Produces: `SearchResult`, `WikiSearchIndex.open(database: Path, *, wal: bool)`, `.search(query: str, limit: int) -> list[SearchResult]`, `.sync(pages: Iterable[WikiPage]) -> None`, `.rebuild(pages: Iterable[WikiPage]) -> None`, `bounded_markdown_search(...)`.

- [ ] **Step 1: Write failing English/Chinese/short-query/recovery tests**

```python
def test_trigram_finds_chinese_substring(index, pages):
    index.rebuild(pages)
    assert index.search("并发写入", 5)[0].logical_path == "concepts/atomic-writes.md"

def test_short_query_uses_title_tag_fallback(index, pages):
    index.rebuild(pages)
    assert {r.logical_path for r in index.search("锁", 5)} == {"concepts/atomic-writes.md"}

def test_corrupt_database_rebuilds_from_markdown(manager):
    manager.layout.database.write_bytes(b"not sqlite")
    assert manager.search("atomic", 5)
```

- [ ] **Step 2: Run tests and verify red**

Run: `cd kimi-cli && uv run pytest tests/wiki/test_search.py tests/wiki/test_search_recovery.py -q`

Expected: FAIL because search cache APIs are missing.

- [ ] **Step 3: Implement FTS5/trigram capability detection and fallback**

```python
def _create_fts(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5("
            "logical_path UNINDEXED,title,tags,summary,body,tokenize='trigram')"
        )
        return True
    except sqlite3.OperationalError:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5("
            "logical_path UNINDEXED,title,tags,summary,body)"
        )
        return False

def search(self, query: str, limit: int) -> list[SearchResult]:
    limit = max(1, min(limit, 20))
    if len(query) < 3 or not self.trigram:
        exact = self._title_tag_search(query, limit)
        if exact:
            return exact
    return self._fts_or_escaped_like(query, limit)
```

Create a content table with `logical_path`, `content_hash`, and `revision`, an FTS table, deterministic `ORDER BY bm25(...), logical_path`, bounded escaped `LIKE`, snippets, and `PRAGMA journal_mode=WAL` only when configured. On missing/stale/corrupt DB, rename it to a temporary diagnostic name, rebuild from validated Markdown, then remove the corrupt cache. Never modify Markdown.

- [ ] **Step 4: Run tests and verify green**

Run: `cd kimi-cli && uv run pytest tests/wiki/test_search.py tests/wiki/test_search_recovery.py -q`

Expected: PASS with FTS5; a monkeypatched “no trigram” connection passes fallback tests; corrupt/deleted/stale cache rebuilds.

- [ ] **Step 5: Commit**

```bash
git add kimi-cli/src/kimi_cli/wiki/search.py kimi-cli/tests/wiki/test_search.py kimi-cli/tests/wiki/test_search_recovery.py
git commit -m "feat: index global wiki with fts5"
```

### Task 6: Cross-Process Lock, Durable Transaction, and Recovery

**Files:**
- Create: `kimi-cli/src/kimi_cli/wiki/locking.py`
- Create: `kimi-cli/src/kimi_cli/wiki/transaction.py`
- Create: `kimi-cli/tests/wiki/test_locking.py`
- Create: `kimi-cli/tests/wiki/test_transaction.py`
- Create: `kimi-cli/tests/wiki/test_recovery.py`

**Interfaces:**
- Consumes: `WikiLayout`, validated `PageChange`, content hashes and revisions.
- Produces: `WikiLock.shared(timeout: float)`, `WikiLock.exclusive(timeout: float)`, `WikiTransaction.prepare(...)`, `.commit() -> int`, `recover_transactions(layout: WikiLayout) -> RecoveryResult`.

- [ ] **Step 1: Write failing serialization and fault-injection tests**

```python
@pytest.mark.parametrize("failpoint", [
    "journal_fsync", "page_replace", "index_replace", "log_replace", "revision_replace"
])
def test_recovery_never_exposes_partial_commit(wiki, failpoint, monkeypatch):
    inject_failure(transaction_module, failpoint, monkeypatch)
    with pytest.raises(OSError):
        wiki.commit(FIXTURE_CHANGE)
    recover_transactions(wiki.layout)
    assert snapshot(wiki) in {BEFORE_SNAPSHOT, AFTER_SNAPSHOT}

def test_approval_is_not_waited_under_writer_lock(fake_approval, manager):
    manager.propose(FIXTURE_CHANGE, fake_approval)
    assert fake_approval.called_before(manager.lock.exclusive_acquired)
```

- [ ] **Step 2: Run tests and verify red**

Run: `cd kimi-cli && uv run pytest tests/wiki/test_locking.py tests/wiki/test_transaction.py tests/wiki/test_recovery.py -q`

Expected: FAIL because lock/journal/recovery implementations are absent.

- [ ] **Step 3: Implement shared/exclusive locking and ordered durable commit**

```python
def _durable_replace(target: Path, data: bytes) -> None:
    fd, raw = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, target)
        fsync_directory(target.parent)
    finally:
        temp.unlink(missing_ok=True)

def commit(self) -> int:
    self._write_prepared_journal_with_backups()
    for change in self.content_changes:
        _durable_replace(change.target, change.new_bytes)
    _durable_replace(self.layout.index, self.index_bytes)
    _durable_replace(self.layout.log, self.log_bytes)
    _durable_replace(self.layout.revision, f"{self.new_revision}\n".encode())
    self._mark_committed()
    return self.new_revision
```

Use `fcntl.flock(LOCK_SH/LOCK_EX)` on POSIX and `msvcrt.locking` on Windows with retry/deadline. Journal JSON records transaction ID, state, old/new hashes, expected page/global revisions, backup paths, and ordered targets. Recovery under exclusive lock discards untouched prepared transactions, rolls forward hash-consistent partial replacements, restores backups when forward completion is impossible, rebuilds FTS after committed Markdown/index/log/revision, and quarantines unreadable journals while allowing read-only access.

- [ ] **Step 4: Run tests and verify green**

Run: `cd kimi-cli && uv run pytest tests/wiki/test_locking.py tests/wiki/test_transaction.py tests/wiki/test_recovery.py -q`

Expected: PASS on the host platform; multiprocess writers serialize, readers see only complete revisions, and every injected interruption recovers to a complete before/after state.

- [ ] **Step 5: Commit**

```bash
git add kimi-cli/src/kimi_cli/wiki/locking.py kimi-cli/src/kimi_cli/wiki/transaction.py kimi-cli/tests/wiki/test_locking.py kimi-cli/tests/wiki/test_transaction.py kimi-cli/tests/wiki/test_recovery.py
git commit -m "feat: make wiki commits recoverable"
```

### Task 7: WikiManager, Value Gate, Merge, Audit, and Lint

**Files:**
- Create: `kimi-cli/src/kimi_cli/wiki/manager.py`
- Create: `kimi-cli/src/kimi_cli/wiki/value_gate.py`
- Create: `kimi-cli/src/kimi_cli/wiki/lint.py`
- Create: `kimi-cli/tests/wiki/test_manager.py`
- Create: `kimi-cli/tests/wiki/test_value_gate.py`
- Create: `kimi-cli/tests/wiki/test_lint.py`

**Interfaces:**
- Consumes: Tasks 2–6.
- Produces: `WikiManager.ensure()`, `.search(query, limit)`, `.read(page)`, `.prepare(candidate, context) -> PreparedWikiChange | DiscardedCandidate`, `.commit(prepared) -> CommitResult`, `.ingest(source, instructions, context)`, `.lint(scope) -> LintReport`.

- [ ] **Step 1: Write failing value, duplicate, conflict, and audit tests**

```python
@pytest.mark.parametrize("candidate,reason", [
    (LOW_VALUE, "low_value"), (UNGROUNDED, "ungrounded"),
    (SECRET_BEARING, "sensitive"), (DUPLICATE, "duplicate"),
])
def test_gate_discards_without_queue(manager, candidate, reason):
    result = manager.prepare(candidate, CONTEXT)
    assert result.reason == reason
    assert not (manager.layout.metadata / "pending").exists()

def test_conflict_preserves_both_sourced_positions(manager):
    result = manager.commit(manager.prepare(CONFLICTING, CONTEXT))
    text = manager.read("concepts/cache-mode.md").content
    assert "Conflict" in text and "source-a" in text and "source-b" in text
    assert f"revision={result.global_revision}" in manager.layout.log.read_text()
```

- [ ] **Step 2: Run tests and verify red**

Run: `cd kimi-cli && uv run pytest tests/wiki/test_manager.py tests/wiki/test_value_gate.py tests/wiki/test_lint.py -q`

Expected: FAIL because manager, gate, and lint APIs do not exist.

- [ ] **Step 3: Implement the application service boundary**

```python
class WikiManager:
    def __init__(self, root: Path | None = None, *, wal: bool = True):
        self.layout = ensure_wiki(root)
        self.registry = WorkspaceRegistry(self.layout.metadata / "workspaces.json")
        self.lock = WikiLock(self.layout.metadata / "locks" / "writer.lock")
        recover_transactions(self.layout)
        self.search_index = WikiSearchIndex.open(self.layout.database, wal=wal)

    def ensure(self) -> WikiLayout:
        return self.layout

    def prepare(self, candidate: WikiCandidate, context: WikiContext):
        gated = evaluate_candidate(candidate, context, self.search(candidate.summary, 10))
        if not gated.accepted:
            return DiscardedCandidate(reason=gated.reason)
        return build_revision_checked_change(gated, self._read_pages())

    def commit(self, prepared: PreparedWikiChange) -> CommitResult:
        with self.lock.exclusive(timeout=5.0):
            current = self._revalidate_or_merge(prepared)
            revision = WikiTransaction.from_change(self.layout, current).commit()
            self.search_index.sync(current.pages)
            return CommitResult(global_revision=revision, pages=current.logical_paths)
```

The deterministic gate requires explicit structured evidence, cross-turn utility, stability, novelty, and safety; it does not call an extra model at session end. Merge duplicates, preserve sourced contradictions in a conflict section, increment each affected page revision, rebuild category-grouped `index.md`, append one machine-parseable `log.md` line last, and discard medium/low/denied candidates in memory. `lint` reports orphan/dead links, duplicate hashes/claims, conflict markers, malformed pages, and missing provenance without modifying content.

- [ ] **Step 4: Run tests and verify green**

Run: `cd kimi-cli && uv run pytest tests/wiki/test_manager.py tests/wiki/test_value_gate.py tests/wiki/test_lint.py -q`

Expected: PASS; revisions are monotonic, conflicts retain both sources, log contains no absolute paths/transcripts, and no pending-review directory exists.

- [ ] **Step 5: Commit**

```bash
git add kimi-cli/src/kimi_cli/wiki/manager.py kimi-cli/src/kimi_cli/wiki/value_gate.py kimi-cli/src/kimi_cli/wiki/lint.py kimi-cli/tests/wiki/test_manager.py kimi-cli/tests/wiki/test_value_gate.py kimi-cli/tests/wiki/test_lint.py
git commit -m "feat: add global wiki manager"
```

### Task 8: Dedicated Wiki Tool and Managed-Root File Guard

**Files:**
- Create: `kimi-cli/src/kimi_cli/tools/wiki.py`
- Create: `kimi-cli/tests/tools/test_wiki_tool.py`
- Modify: `kimi-cli/src/kimi_cli/tools/file/write.py`
- Modify: `kimi-cli/src/kimi_cli/tools/file/replace.py`
- Modify: `kimi-cli/tests/tools/test_write_file.py`
- Modify: `kimi-cli/tests/tools/test_str_replace_file.py`
- Modify: `kimi-cli/src/kimi_cli/agents/default/agent.yaml`

**Interfaces:**
- Consumes: `Runtime.wiki`, `WikiManager` operations, current session/workspace context, existing `Approval.request`.
- Produces: `Wiki(CallableTool2[Params])` with `operation: Literal["search","read","remember","ingest","lint"]`.

- [ ] **Step 1: Write failing operation, ingest-boundary, and bypass tests**

```python
async def test_ingest_rejects_directory_archive_and_outside_path(wiki_tool, tmp_path):
    for source in [str(tmp_path), str(tmp_path / "wiki.zip"), "/etc/passwd"]:
        result = await wiki_tool(Params(operation="ingest", source=source))
        assert result.is_error

async def test_file_tools_cannot_write_managed_wiki(write_file, wiki_root):
    result = await write_file(WriteParams(path=str(wiki_root / "index.md"), content="x"))
    assert result.is_error and "Wiki tool" in result.message
```

- [ ] **Step 2: Run tests and verify red**

Run: `cd kimi-cli && uv run pytest tests/tools/test_wiki_tool.py tests/tools/test_write_file.py tests/tools/test_str_replace_file.py -q`

Expected: FAIL because `Wiki` tool/runtime dependency and managed-root guard are absent.

- [ ] **Step 3: Implement one controlled tool and explicit file guard**

```python
class Params(BaseModel):
    operation: Literal["search", "read", "remember", "ingest", "lint"]
    query: str | None = None
    page: str | None = None
    candidate: WikiCandidate | None = None
    source: CurrentSource | None = None
    instructions: str | None = None
    limit: int = Field(default=5, ge=1, le=20)

class Wiki(CallableTool2[Params]):
    name = "Wiki"
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
    async def __call__(self, params: Params) -> ToolReturnValue:
        return await dispatch_wiki_operation(self.runtime, params)
```

`search`, `read`, and `lint` are read-only. `remember` and `ingest` prepare structured changes and pass them to the permission path in Task 10. `ingest` accepts inline current-turn user content or a file represented by workspace UUID plus relative path after registry containment; reject directories, archives, arbitrary paths, URLs not already represented as an allowed current-turn source, and sensitive material. Add a shared `reject_managed_wiki_target(path, wiki_root)` check before approval in both file mutation tools.

- [ ] **Step 4: Run tests and verify green**

Run: `cd kimi-cli && uv run pytest tests/tools/test_wiki_tool.py tests/tools/test_write_file.py tests/tools/test_str_replace_file.py -q`

Expected: PASS; tool cannot act as directory import and ordinary file tools cannot mutate any descendant or symlink alias of the Wiki root.

- [ ] **Step 5: Commit**

```bash
git add kimi-cli/src/kimi_cli/tools/wiki.py kimi-cli/tests/tools/test_wiki_tool.py kimi-cli/src/kimi_cli/tools/file/write.py kimi-cli/src/kimi_cli/tools/file/replace.py kimi-cli/tests/tools/test_write_file.py kimi-cli/tests/tools/test_str_replace_file.py kimi-cli/src/kimi_cli/agents/default/agent.yaml
git commit -m "feat: expose controlled wiki tool"
```

### Task 9: Bounded Session Awareness and Shared Runtime Wiring

**Files:**
- Create: `kimi-cli/src/kimi_cli/wiki/context.py`
- Create: `kimi-cli/tests/wiki/test_context.py`
- Modify: `kimi-cli/src/kimi_cli/soul/agent.py`
- Modify: `kimi-cli/src/kimi_cli/agents/default/system.md`
- Modify: `kimi-cli/tests/core/test_agent.py`

**Interfaces:**
- Consumes: `WikiManager`, current `Session.work_dir`, `WorkspaceRegistry`.
- Produces: `render_compact_index(index_text: str, *, max_bytes: int = 8192, max_entries: int = 80, hints: Sequence[str] = ()) -> str`; `Runtime.wiki: WikiManager | None`; `BuiltinSystemPromptArgs.KIMI_WIKI_CONTEXT: str`.

- [ ] **Step 1: Write failing UTF-8 limits and resilient session tests**

```python
def test_compact_index_preserves_utf8_entries_and_marker():
    rendered = render_compact_index(CHINESE_INDEX, max_bytes=120, max_entries=2)
    assert len(rendered.encode("utf-8")) <= 120
    assert rendered.endswith("<!-- Wiki index truncated -->")
    rendered.encode("utf-8").decode("utf-8")

async def test_wiki_failure_does_not_block_runtime(monkeypatch, runtime_args):
    monkeypatch.setattr(WikiManager, "ensure", Mock(side_effect=OSError("disk")))
    runtime = await Runtime.create(**runtime_args)
    assert runtime.wiki is None
    assert runtime.builtin_args.KIMI_WIKI_CONTEXT == ""
```

- [ ] **Step 2: Run tests and verify red**

Run: `cd kimi-cli && uv run pytest tests/wiki/test_context.py tests/core/test_agent.py -q`

Expected: FAIL because compact rendering and runtime Wiki fields are absent.

- [ ] **Step 3: Initialize one global manager and inject bounded awareness**

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class BuiltinSystemPromptArgs:
    # existing fields retained
    KIMI_WIKI_CONTEXT: str

# Runtime.create
try:
    wiki = await asyncio.to_thread(WikiManager)
    workspace_id = await asyncio.to_thread(wiki.registry.register, work_dir_local)
    compact = await asyncio.to_thread(
        render_compact_index, wiki.layout.index.read_text(encoding="utf-8"),
        hints=(work_dir_local.name,),
    )
    wiki_context = (
        "The global Wiki is shared across all workspaces. Use Wiki search/read "
        "for durable knowledge. Propose only durable, sourced conclusions for writing.\n\n"
        + compact
    )
except Exception:
    logger.exception("Global Wiki unavailable; continuing without Wiki")
    wiki = None; workspace_id = None; wiki_context = ""
```

Store `wiki` and `workspace_id` on `Runtime`, share the same references in `copy_for_subagent`, add `${KIMI_WIKI_CONTEXT}` under a conditional “Global Wiki” section in `system.md`, and retain the existing workspace-local knowledge block only as a compatibility input until Task 11 migrates its scope. Rank hinted whole index entries before truncation, cap both 8 KiB and 80 entries, and reserve bytes for the marker.

- [ ] **Step 4: Run tests and verify green**

Run: `cd kimi-cli && uv run pytest tests/wiki/test_context.py tests/core/test_agent.py -q`

Expected: PASS; two sessions in unrelated temporary workspaces share one root and initialization failure still creates both sessions with one concise user diagnostic.

- [ ] **Step 5: Commit**

```bash
git add kimi-cli/src/kimi_cli/wiki/context.py kimi-cli/tests/wiki/test_context.py kimi-cli/src/kimi_cli/soul/agent.py kimi-cli/src/kimi_cli/agents/default/system.md kimi-cli/tests/core/test_agent.py
git commit -m "feat: add global wiki session awareness"
```

### Task 10: Compact Approval and YOLO Write Policy

**Files:**
- Modify: `kimi-cli/src/kimi_cli/tools/wiki.py`
- Modify: `kimi-cli/src/kimi_cli/soul/approval.py`
- Modify: `kimi-cli/src/kimi_cli/tools/display.py`
- Create: `kimi-cli/tests/tools/test_wiki_approval.py`
- Modify: `kimi-cli/web/src/features/chat/components/approval-dialog.tsx`
- Modify: `kimi-cli/web/src/i18n/locales/en/chat.json`
- Modify: `kimi-cli/web/src/i18n/locales/zh-CN/chat.json`
- Create: `tests/test_wiki_approval_ui.py`

**Interfaces:**
- Consumes: existing `Approval.request(sender, action, description, display)` and wire responses `approve`, `approve_for_session`, `reject`.
- Produces: action key `wiki.write`; compact Wiki approval metadata encoded in existing `DisplayBlock`; no new approval transport.

- [ ] **Step 1: Write failing normal/session/decline/YOLO tests**

```python
async def test_normal_write_asks_before_lock(runtime, wiki_tool, approval_runtime):
    pending = asyncio.create_task(wiki_tool(REMEMBER_PARAMS))
    request = await approval_runtime.next_pending()
    assert request.action == "wiki.write"
    assert request.description == "Record: Atomic Wiki recovery\nChanges: 2 pages"
    assert not runtime.wiki.lock.exclusive_acquired
    approval_runtime.resolve(request.id, "approve_for_session")
    assert not (await pending).is_error
    assert "wiki.write" in runtime.session.state.approval.auto_approve_actions

async def test_yolo_still_rejects_low_value(runtime_yolo, wiki_tool):
    result = await wiki_tool(LOW_VALUE_PARAMS)
    assert result.is_error and runtime_yolo.wiki.global_revision == 0

def test_wiki_approval_reuses_three_actions_with_compact_copy():
    source = Path("kimi-cli/web/src/features/chat/components/approval-dialog.tsx").read_text()
    assert 'type === "wiki"' in source
    assert "approve_for_session" in source
    assert "wikiApproval.details" in source
    assert "machinePath" not in source
```

- [ ] **Step 2: Run tests and verify red**

Run: `cd kimi-cli && uv run pytest tests/tools/test_wiki_approval.py -q && cd .. && kimi-cli/.venv/bin/python -m pytest tests/test_wiki_approval_ui.py -q`

Expected: backend FAIL because Wiki writes do not request scoped approval; source-contract test FAIL because compact Wiki labels/details are absent.

- [ ] **Step 3: Reuse existing Approval flow with minimal UI specialization**

```python
class WikiApprovalBlock(DisplayBlock):
    type: str = "wiki"
    summary: str
    pages: list[str]
    workspace_id: str | None
    session_id: str
    details: list[str]

prepared = self.runtime.wiki.prepare(params.candidate, self._context())
if isinstance(prepared, DiscardedCandidate):
    return ToolError(message=prepared.reason, brief="Wiki candidate discarded")
if not self.runtime.approval.is_yolo():
    result = await self.runtime.approval.request(
        self.name,
        "wiki.write",
        f"Record: {prepared.summary}\nChanges: {len(prepared.pages)} pages",
        display=[WikiApprovalBlock.from_prepared(prepared)],
    )
    if not result:
        return result.rejection_error()
return await asyncio.to_thread(self.runtime.wiki.commit, prepared)
```

Keep existing three primary actions. The frontend recognizes only the Wiki display block to show title “Write to global Wiki”, two compact lines, and a collapsed details disclosure containing logical paths/provenance IDs—never absolute paths or chat transcripts. Do not change unrelated Approval presentation. Delivery failure/cancellation returns rejection and commits nothing. AFK retains existing Approval semantics; only `is_yolo()` authorizes proactive Wiki writes.

- [ ] **Step 4: Run tests and verify green**

Run: `cd kimi-cli && uv run pytest tests/tools/test_wiki_approval.py -q && cd .. && kimi-cli/.venv/bin/python -m pytest tests/test_wiki_approval_ui.py -q`

Expected: PASS for allow once, session-only `wiki.write`, decline, delivery failure, explicit-user normal write, model-initiated normal proposal, and YOLO gate/audit behavior.

- [ ] **Step 5: Commit**

```bash
git add kimi-cli/src/kimi_cli/tools/wiki.py kimi-cli/src/kimi_cli/soul/approval.py kimi-cli/src/kimi_cli/tools/display.py kimi-cli/tests/tools/test_wiki_approval.py kimi-cli/web/src/features/chat/components/approval-dialog.tsx kimi-cli/web/src/i18n/locales/en/chat.json kimi-cli/web/src/i18n/locales/zh-CN/chat.json tests/test_wiki_approval_ui.py
git commit -m "feat: gate global wiki writes"
```

### Task 11: Knowledge API Authorization and Global-Scope Migration

**Files:**
- Modify: `kimi-cli/src/kimi_cli/web/api/memory.py`
- Modify: `kimi-cli/src/kimi_cli/memory/knowledge.py`
- Create: `kimi-cli/tests/web/test_global_knowledge_api.py`
- Create: `kimi-cli/tests/web/test_memory_api.py`
- Modify: `kimi-cli/web/src/hooks/useMemory.ts`
- Modify: `kimi-cli/web/src/features/memory/memory-panel.tsx`

**Interfaces:**
- Consumes: `WikiManager` global scope and current local authenticated namespace.
- Produces: legacy Knowledge API compatibility without using arbitrary `session_id` as authority; no Admin Panel changes.

- [ ] **Step 1: Write failing cross-workspace authorization tests**

```python
def test_session_uuid_cannot_read_other_workspace_legacy_knowledge(client, users, sessions):
    response = client.get(
        f"/api/memory/knowledge?session_id={sessions.other_user.id}",
        headers=users.alice.headers,
    )
    assert response.status_code == 403

def test_global_knowledge_is_same_across_owned_sessions(client, users, sessions):
    a = client.get("/api/memory/knowledge", headers=users.alice.headers).json()
    b = client.get("/api/memory/knowledge", headers=users.alice.headers).json()
    assert a == b
```

- [ ] **Step 2: Run tests and verify red**

Run: `cd kimi-cli && uv run pytest tests/web/test_global_knowledge_api.py tests/web/test_memory_api.py -q`

Expected: FAIL because current Knowledge reads derive a workspace directory from any supplied session UUID.

- [ ] **Step 3: Resolve global scope first and authorize legacy session resources separately**

```python
def _require_session_access(session_id: UUID, user: dict[str, Any]) -> SessionRecord:
    session = load_session_by_id(session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    if user.get("role") != "admin" and session.owner_id not in (None, user["id"]):
        raise HTTPException(403, "Session access denied")
    return session

@router.get("/knowledge")
async def list_knowledge(user=Depends(require_current_user)):
    manager = WikiManager()
    return [
        KnowledgeFile(
            name=page.logical_path,
            size=len(render_page(page).encode("utf-8")),
            mtime=page.updated.timestamp(),
        )
        for page in manager.list_pages()
    ]
```

Make global endpoints independent of `session_id`; if a backward-compatible request supplies it, validate ownership before ignoring it. Route reads through validated logical page names and manager shared locks. Route mutations through the managed Wiki write boundary or mark old direct PUT/DELETE as `409 Use Wiki tool` so the HTTP API cannot bypass Approval/revision/audit. Update the non-admin Memory panel hook to stop passing `session_id`; do not touch `admin-page.tsx` or `admin-knowledge-panel.tsx`.

- [ ] **Step 4: Run backend and frontend checks**

Run: `cd kimi-cli && uv run pytest tests/web/test_global_knowledge_api.py tests/web/test_memory_api.py -q && cd web && npm run typecheck`

Expected: PASS; cross-owner UUID returns 403, all owned workspaces see the same global catalog, and TypeScript reports no errors.

- [ ] **Step 5: Commit**

```bash
git add kimi-cli/src/kimi_cli/web/api/memory.py kimi-cli/src/kimi_cli/memory/knowledge.py kimi-cli/tests/web/test_global_knowledge_api.py kimi-cli/tests/web/test_memory_api.py kimi-cli/web/src/hooks/useMemory.ts kimi-cli/web/src/features/memory/memory-panel.tsx
git commit -m "fix: secure global knowledge scope"
```

### Task 12: Docker/KAOS, Packaging, No-End-Hook, and Full Verification

**Files:**
- Create: `kimi-cli/tests/wiki/test_deployment.py`
- Create: `kimi-cli/tests/wiki/test_no_session_end_archive.py`
- Modify: `kimi-cli/src/kimi_cli/web/runner/worker.py`
- Modify: `kimi-cli/src/kimi_cli/web/runner/container.py`
- Modify: `kimi-cli/src/kimi_cli/acp/kaos.py`
- Modify: `packaging/build_windows.py`
- Modify: `packaging/README.md`
- Modify: `tests/test_windows_packaging.py`
- Modify: `tests/test_work_directory_settings.py`
- Modify: `docs/superpowers/specs/2026-07-24-global-user-wiki-design.md`

**Interfaces:**
- Consumes: `OPENKIMO_WIKI_ROOT`, `WikiManager(wal=...)`, existing worker environment construction.
- Produces: identical configured Wiki root across local sessions/workspaces; explicit shared-volume behavior through `OPENKIMO_WIKI_CACHE_MODE=shared-local|process-local|disabled`.

- [ ] **Step 1: Write failing deployment, package, and no-end-hook tests**

```python
def test_all_workers_receive_same_wiki_root(worker_envs, tmp_path):
    assert {env["OPENKIMO_WIKI_ROOT"] for env in worker_envs} == {str(tmp_path / "wiki")}

async def test_session_shutdown_never_writes_or_calls_model(runtime, spy_wiki, spy_llm):
    await runtime.background_tasks.shutdown()
    runtime.session.close()
    assert spy_wiki.commits == []
    assert spy_llm.calls == []

def test_windows_package_contains_only_generic_templates(staging):
    templates = staging / "runtime/kimi_cli/kimi_cli/wiki/templates"
    assert (templates / "schema.md").is_file()
    assert "local_agent_work" not in "\n".join(p.read_text() for p in templates.iterdir())
```

- [ ] **Step 2: Run tests and verify red**

Run: `kimi-cli/.venv/bin/python -m pytest kimi-cli/tests/wiki/test_deployment.py kimi-cli/tests/wiki/test_no_session_end_archive.py tests/test_windows_packaging.py tests/test_work_directory_settings.py -q`

Expected: FAIL because worker/cache-mode propagation and package assertions are not implemented.

- [ ] **Step 3: Wire explicit deployment modes without adding a service**

```python
def wiki_environment(base: dict[str, str]) -> dict[str, str]:
    env = dict(base)
    env["OPENKIMO_WIKI_ROOT"] = os.environ["OPENKIMO_WIKI_ROOT"]
    env.setdefault("OPENKIMO_WIKI_CACHE_MODE", "shared-local")
    return env

def wal_enabled(cache_mode: str) -> bool:
    return cache_mode == "shared-local"
```

Pass the identical root into local worker/container/ACP processes. For `process-local`, place SQLite under the worker's existing cache directory and rebuild from shared Markdown; for `disabled`, use bounded Markdown fallback and never create SQLite. A read-only mounted Wiki permits search/read and returns a clear error for remember/ingest. Ensure no shutdown/close/compact/archive callback imports `WikiManager.commit` or invokes the LLM. Add a design note that future login work must map authenticated user IDs to separate namespaces and migrate `default` explicitly; do not implement identity selection now.

- [ ] **Step 4: Run focused integration, security, performance, and concurrency suites**

Run: `cd kimi-cli && uv run pytest tests/wiki tests/tools/test_wiki_tool.py tests/tools/test_wiki_approval.py tests/web/test_global_knowledge_api.py -q`

Expected: PASS, including 20 concurrent readers/4 writers, a 1,000-page fixture search under 200 ms after warmup, 8 KiB compact-index bound, lock timeout, read-only mount, symlink escape, secret rejection, corrupt cache rebuild, and no session-end writes.

- [ ] **Step 5: Run complete backend/frontend/package verification**

Run:

```bash
cd kimi-cli
uv run pytest tests -q
uv run pyright src/kimi_cli/wiki src/kimi_cli/tools/wiki.py src/kimi_cli/soul/agent.py src/kimi_cli/web/api/memory.py
uv run ruff check src/kimi_cli/wiki src/kimi_cli/tools/wiki.py tests/wiki
cd web
npm run typecheck
npm run build
cd ../..
kimi-cli/.venv/bin/python -m pytest tests -q
git diff --check
git status --short
```

Expected: all pytest suites PASS; Pyright reports `0 errors`; Ruff reports `All checks passed!`; Vite build succeeds; `git diff --check` is silent; status shows only intended Wiki changes plus the pre-existing unstaged `packaging/venvstacks.resolved.toml`.

- [ ] **Step 6: Commit deployment and verification changes without staging the user file**

```bash
git add kimi-cli/tests/wiki/test_deployment.py kimi-cli/tests/wiki/test_no_session_end_archive.py kimi-cli/src/kimi_cli/web/runner/worker.py kimi-cli/src/kimi_cli/web/runner/container.py kimi-cli/src/kimi_cli/acp/kaos.py packaging/build_windows.py packaging/README.md tests/test_windows_packaging.py tests/test_work_directory_settings.py docs/superpowers/specs/2026-07-24-global-user-wiki-design.md
git diff --cached --name-only | grep -v '^packaging/venvstacks.resolved.toml$'
git commit -m "test: verify global wiki deployment"
```

The `grep` output must list every staged path and must not list `packaging/venvstacks.resolved.toml`. If it does, run `git restore --staged packaging/venvstacks.resolved.toml` before committing.

## Final Acceptance Checklist

- [ ] The repository/package contains generic templates only and no existing private Wiki content.
- [ ] macOS, Windows, local CLI, Docker, and KAOS resolve the intended shared user-level Wiki.
- [ ] Every workspace shares Markdown while provenance stays UUID-relative and relocatable.
- [ ] Page paths, frontmatter, links, revisions, sources, and secrets are validated.
- [ ] FTS5/trigram/BM25, short-query fallback, WAL modes, rebuild, and Markdown fallback pass.
- [ ] Atomic content/index/log/revision ordering, locks, journal, fsync, replace, and crash recovery pass.
- [ ] `Wiki` search/read/remember/ingest/lint obey boundaries; ingest is not import.
- [ ] Compact index and two/three guidance sentences stay within the strict prompt budget.
- [ ] Normal/YOLO write policy, compact Approval, value gate, discard behavior, and audit pass.
- [ ] No session-end hook performs Wiki work.
- [ ] Knowledge scope authorization is fixed without Admin Panel changes.
- [ ] No embedding, daemon, migration report, pending queue, directory import, or multi-user implementation exists.
- [ ] `packaging/venvstacks.resolved.toml` remains unstaged and unmodified by this implementation.
