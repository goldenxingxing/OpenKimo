# Global User Wiki Design

## Goal

Give one local OpenKimo user a durable Markdown wiki that is available from every
workspace immediately after installation. The design productizes the general
structure of the existing `local_agent_work/AGENTS.md` and follows the persistent,
compounding knowledge-base pattern described by
[Karpathy's LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f),
without packaging or migrating any existing private wiki pages.

This design is intentionally local-first. Markdown is the only authoritative data.
Search indexes and runtime state are disposable derivatives.

## Scope

This release provides:

- one software-managed wiki shared by every workspace of the local user;
- an installation-time, platform-neutral wiki skeleton and productized schema;
- bounded wiki awareness in every session through a compact index and short tool
  guidance;
- controlled search, read, source ingestion, and high-value writing;
- existing Approval UI integration for writes outside YOLO mode;
- local full-text search, concurrent-write protection, audit history, and recovery;
- stable workspace references that survive directory moves.

The implementation does not add a standalone runtime, service, watcher, or daemon.
`WikiManager` is an ordinary module hosted by the existing OpenKimo backend. Every
session resolves the same managed wiki root and calls the same module-level service
boundary.

## Explicit non-goals

This release does not:

- change the Admin Panel or add a Wiki tab;
- copy, package, import, scan, or migrate any page from the existing
  `local_agent_work/wiki` directory;
- produce a migration report;
- expose a user-facing directory or ZIP import feature;
- archive or summarize a session automatically when it ends;
- maintain a pending-review queue;
- add vector embeddings, a vector database, or model-based reranking;
- implement accounts, login, multiple users, sharing, or per-user authorization;
- require Obsidian, a parent-directory `AGENTS.md`, a particular agent tool name, or
  mandatory subagent orchestration;
- allow ordinary filesystem tools to mutate the managed wiki.

## Productizing the existing schema

The existing `/Users/qunwei/Documents/local_agent_work/AGENTS.md` is reference
material for the general wiki model only. Its business-independent concepts are
retained:

- interlinked Markdown pages;
- YAML frontmatter and page revisions;
- entity, concept, comparison, source, query, and lint page categories;
- `index.md` as a content-oriented catalog;
- `overview.md` as a high-level synthesis;
- append-only `log.md` as an audit trail;
- explicit source provenance;
- contradiction preservation instead of silent overwrite;
- linting for orphan pages, dead links, stale claims, and conflicts.

The packaged schema is a new product-level `schema.md`, not a copy of `AGENTS.md`.
It removes assumptions tied to the current development environment:

- no absolute path under `/Users/qunwei/Documents/local_agent_work`;
- no assumption that the wiki is below the current workspace;
- no reliance on instruction inheritance from a parent directory;
- no instruction to open or use Obsidian;
- no references to `create_subagent`, `task`, `TaskOutput`, or any other
  environment-specific tool name;
- no rule requiring a root agent to delegate all concrete work;
- no workspace-local `output/`, raw-source, or coordination directory assumption;
- no speculative multi-agent lease protocol in the content schema.

The misspelled `comparations` path is normalized to `comparisons` everywhere.
Wiki links and all internal paths are relative to the managed wiki root.

No existing entity, concept, query, source, comparison, overview content, or other
business page is copied into the repository or application package.

## Data location and namespace

The wiki lives in the platform's per-user application-data location. The exact base
directory is resolved by the same desktop path abstraction used for other managed
OpenKimo data:

```text
<OpenKimo application data>/
└── users/
    └── default/
        └── wiki/
```

On macOS the base is the user's Application Support directory. On Windows it is the
appropriate system-resolved per-user application-data directory; it is never
constructed from a hard-coded username. Tests override the base directory rather
than writing to the real user profile.

`users/default` is a migration seam, not a multi-user implementation. The current
backend always selects `default`. When login and multi-user support is developed,
the wiki must move to one namespace per authenticated user so that:

- one user's workspaces continue to share one wiki;
- different users cannot search, read, or write each other's wiki;
- the current `default` namespace can be assigned or migrated explicitly;
- authorization is enforced before resolving a user's wiki root.

That multi-user work is deliberately deferred and must be developed together with
the login feature, rather than partially simulated in this release.

## Authoritative directory layout

First launch creates this empty, generic structure:

```text
wiki/
├── schema.md
├── index.md
├── overview.md
├── log.md
├── entities/
├── concepts/
├── comparisons/
├── sources/
├── queries/
├── lint/
└── .openkimo/
    ├── manifest.json
    ├── workspaces.json
    ├── revision
    ├── journal/
    ├── locks/
    └── search.sqlite3
```

`schema.md`, `index.md`, `overview.md`, and `log.md` are initialized from generic
package templates. The category directories are empty. `.openkimo` contains
implementation metadata and the rebuildable search cache; it is not wiki content.

Initialization is idempotent:

1. resolve and validate the managed root;
2. create missing directories without replacing existing ones;
3. create a missing special file from its current template;
4. validate the manifest schema version;
5. apply narrowly versioned schema migrations only to software-owned metadata;
6. open or rebuild the search index when necessary.

An application upgrade never overwrites user-edited Markdown merely because a newer
template exists. Template/schema evolution uses an explicit version migration with
conflict checks.

## Page schema

Every content page uses YAML frontmatter with stable fields:

```yaml
---
title: "Page title"
created: "2026-07-24T12:00:00+08:00"
updated: "2026-07-24T12:00:00+08:00"
tags: [example]
sources:
  - kind: workspace-file
    workspace_id: "stable-uuid"
    path: "docs/example.md"
    content_hash: "sha256:..."
revision: 1
---
```

Source entries may also represent user-provided conversation text or a web URL. A
source records enough provenance to review a claim but never stores an API key,
credential, session cookie, private environment value, or machine-specific absolute
path in page content.

Wiki links use `[[category/slug]]`. Logical page paths are limited to the declared
categories and normalized slugs. Page revisions increase monotonically. A new source
that conflicts with an established claim records both positions and their sources;
it does not silently erase the older claim.

`index.md` contains category-grouped links and one-line summaries. `overview.md`
contains a maintained high-level synthesis. Each `log.md` record begins with a
machine-parseable timestamp and identifies the operation, affected logical pages,
source workspace/session, and resulting global revision. The audit log does not
include private absolute paths or full conversation transcripts.

## Workspace identity and portable provenance

Each workspace receives a stable UUID. The local registry maps that UUID to the
currently known canonical absolute path:

```json
{
  "schema_version": 1,
  "workspaces": {
    "uuid": {
      "path": "/platform/specific/current/path",
      "last_seen_at": "2026-07-24T12:00:00+08:00"
    }
  }
}
```

Wiki pages store only the workspace UUID and path relative to that workspace. If a
workspace moves, only the registry mapping changes. Resolution must verify that the
joined and canonicalized path remains below the registered workspace root before a
source can be opened.

An unmapped or missing workspace reference remains useful historical provenance but
is non-executable. The tool reports that the source needs relocation; it must not
guess an absolute path.

## Session awareness

Every session, regardless of work directory, obtains Wiki capability from the same
managed root. Session setup adds:

1. a strictly size-limited compact rendering of `index.md`; and
2. two or three sentences explaining that Wiki tools can search/read global
   knowledge and that durable, sourced conclusions may be proposed for writing.

The compact index is not the entire Wiki and must not grow without bound. The
implementation defines both byte and entry limits, preserves whole UTF-8 entries,
prioritizes titles and one-line summaries relevant to the session/workspace when
available, and includes a truncation marker. Full page content is retrieved only
through search and read calls.

Failure to initialize or query the Wiki must not prevent session creation. The
session continues without injected Wiki context and surfaces one concise diagnostic;
the backend records the detailed error.

## Search architecture

Markdown files are the only source of truth. SQLite is an embedded, disposable
search cache:

- [SQLite FTS5](https://www.sqlite.org/fts5.html) indexes logical path, title, tags,
  summary, and body;
- BM25 provides the initial rank;
- the FTS5 trigram tokenizer provides substring-friendly Chinese and mixed-language
  matching;
- startup detects FTS5 and trigram support in the bundled SQLite; when trigram is
  unavailable, the cache uses the available tokenizer plus the same bounded
  title/tag and `LIKE` fallbacks instead of failing installation;
- very short queries that trigram cannot serve use exact title/tag matching and an
  escaped, bounded `LIKE` fallback;
- indexed rows include the Markdown content hash and revision;
- stale or corrupt databases are rebuilt entirely from Markdown;
- vector embeddings are deferred until measured retrieval failures justify their
  model, disk, startup, and cross-platform costs.

[SQLite WAL](https://www.sqlite.org/wal.html) allows readers to continue while the
short indexing transaction commits. WAL is an implementation mode, not a second
store. The database, `-wal`, and `-shm` files reside together on a local filesystem.

For Docker or another non-local worker, the application-data wiki root must be
mounted into every process at one identical logical configuration path. Because
SQLite WAL requires cooperating processes to share local host state, deployments on
network filesystems either keep the SQLite cache process-local and rebuildable or
disable WAL/cache sharing; Markdown remains shared and authoritative.

## WikiManager and tool boundary

`WikiManager` owns path validation, initialization, querying, mutations, locking,
revision checks, indexing, and recovery. It exposes application operations rather
than raw filesystem paths.

The model receives one dedicated Wiki tool with these operations:

- `search(query, limit)` searches the global index and returns logical paths,
  summaries, snippets, scores, and revisions;
- `read(page)` reads one validated Markdown page by logical path;
- `remember(candidate)` proposes a sourced, structured high-value change;
- `ingest(source, instructions)` structures source material provided in the current
  user interaction and proposes the resulting wiki changes;
- `lint(scope)` reports orphan links, dead links, duplicate claims, contradictions,
  and stale provenance without silently rewriting pages.

Here `ingest` does not accept a wiki directory, archive, or arbitrary source path and
is not a migration/import facility. It processes content already supplied or
explicitly selected within the current session's permitted workspace boundary.

Ordinary filesystem tools cannot write below the managed wiki root. The root is not
added as a general workspace. All mutations must pass through `WikiManager`, even
when the source is an uploaded file or a model-generated conclusion.

## Write policy and value gate

There is no session-end hook for Wiki persistence. Ending, closing, compacting, or
archiving a session performs no Wiki extraction and invokes no additional model.

Before a write can be proposed, the candidate must be:

- useful beyond the immediate turn or one-off execution;
- a stable conclusion rather than an unverified hypothesis or failed attempt;
- grounded in user confirmation, a workspace artifact, or a reliable cited source;
- materially new or an improvement to existing knowledge;
- free of credentials, secrets, sensitive raw conversation content, and unsafe
  absolute paths;
- structured into the established page schema with explicit provenance.

The gate searches existing pages before proposing a change. Duplicate content is
merged or discarded. Contradictions are preserved with both sources and an explicit
conflict note. Medium- and low-value candidates are discarded immediately; there is
no pending-review storage.

Behavior then follows the current execution mode:

- **Normal mode, model-initiated:** the model may say that a conclusion is worth
  recording and submit a proposal through the existing Approval flow.
- **Normal mode, user explicitly asks to remember:** the clear intent permits the
  proposal, but the actual managed write still uses Approval.
- **YOLO mode:** a candidate that passes the same value and safety gate may be
  written proactively without a popup. It still receives revision, provenance, and
  audit records.

A denied proposal is discarded immediately and never written to a hidden queue.

## Approval experience

Wiki writes reuse the existing page-level Approval request and decision plumbing.
The default popup stays compact:

```text
Write to global Wiki

Record: <one-line summary>
Changes: <number> pages

[Allow once] [Always allow this session] [Decline]
```

An optional collapsed detail section lists logical page paths, source
workspace/session, path normalization, and duplicate/conflict handling. It does not
show machine absolute paths or an entire conversation.

“Always allow this session” applies only to Wiki writes in that session. It neither
changes global mode nor grants arbitrary filesystem access. Approval is resolved
before the writer lock is acquired so a slow user decision cannot block unrelated
sessions.

## Atomic writes, concurrency, and recovery

All mutating operations use a single cross-process global writer lock. Reads use the
corresponding shared lock briefly and resolve only a committed global revision. If a
crashed writer left an unfinished journal, a reader waits for recovery instead of
observing a partial multi-page change. A mutation follows this order:

1. build and validate the complete logical change set in memory;
2. read the expected page revisions and current global revision;
3. request Approval when the execution mode requires it;
4. acquire the writer lock and revalidate all revisions;
5. write a durable transaction journal describing old and new hashes and durable
   rollback copies for every replaced file;
6. write each new page to a sibling temporary file, flush it, and call `fsync`;
7. atomically replace content pages;
8. atomically replace `index.md`;
9. atomically replace `log.md` with its previous content plus the appended record;
10. atomically advance the global revision and mark the journal committed;
11. update the FTS cache in one short SQLite transaction;
12. remove completed journal artifacts after their retention window.

Temporary files stay on the same filesystem as their targets so
[`os.replace`](https://docs.python.org/3/library/os.html#os.replace) can provide
atomic same-filesystem replacement where the platform supports it. Directory and
journal metadata are also flushed where the platform exposes that guarantee.

If expected revisions changed before lock acquisition, the manager rereads and
recomputes the merge. It never overwrites a newer page. An unsafe or ambiguous merge
is cancelled and returned to the session as a conflict.

Recovery examines journals before accepting new writes:

- a prepared transaction with only temporary files is discarded;
- a partially replaced transaction is completed or rolled forward using recorded
  hashes and backups;
- a committed Markdown transaction with a failed FTS update triggers reindexing;
- an unreadable journal quarantines writes but leaves reads available.

SQLite uses its documented
[atomic commit](https://www.sqlite.org/atomiccommit.html) behavior for the search
transaction, but SQLite cannot define the Markdown transaction boundary. The journal
and ordered same-filesystem replacements provide that boundary. `index.md` and
`log.md` are updated after content pages so they never advertise an uncommitted page.

## Knowledge scope and authorization

The new Wiki is user-global, not session-global. Existing Knowledge endpoints must
not accept a session UUID as sufficient authority to read another workspace's
session knowledge. The implementation replaces session-derived Wiki scope with the
resolved global user Wiki scope and separately validates ownership before any
legacy session resource is accessed.

Until login exists, “user” means the one local desktop application-data namespace.
Remote server deployments must configure one shared managed root deliberately and
must not expose that root to unauthenticated clients.

## Failure behavior

The system favors an available session and intact Markdown:

- initialization failure disables Wiki features for that session, not the session;
- FTS open, query, or update failure falls back to bounded Markdown/title search and
  schedules a rebuild;
- malformed Markdown is skipped from search and reported by lint, not deleted;
- a full disk or failed `fsync` aborts before revision advancement;
- lock timeout returns a retryable busy error;
- revision conflict returns a conflict without modifying either version;
- missing workspace source paths remain non-executable provenance;
- a failed Approval delivery or resolution never defaults to allow;
- read-only Docker mounts allow search/read fallback but reject remember/ingest
  clearly.

## Testing strategy

### Initialization and portability

- clean first launch creates exactly the generic skeleton;
- repeated initialization is idempotent and preserves edited Markdown;
- macOS and Windows resolve user-friendly application-data roots without hard-coded
  usernames;
- every work directory and session resolves the same wiki root;
- workspace moves update registry mappings without rewriting pages;
- absolute-path escape, `..`, symlink escape, and unknown workspace references are
  rejected or made non-executable as appropriate;
- repository/package inspection proves that no existing private wiki page is
  shipped.

### Schema and session context

- templates contain only product-level schema and use `comparisons`;
- schema has no Obsidian, parent-inheritance, environment-specific tool, or mandatory
  delegation rule;
- compact-index rendering obeys byte and entry limits for UTF-8 Chinese and English;
- truncation preserves valid Markdown entries and includes a marker;
- a Wiki initialization failure still allows session creation;
- session shutdown produces no Wiki file, model call, or candidate.

### Search

- FTS5 finds English terms, Chinese substrings, tags, and mixed-language queries;
- short terms use bounded title/tag and `LIKE` fallbacks;
- BM25 ranking and result limits are deterministic under fixed fixtures;
- content hash changes update the correct row;
- deleted, corrupt, or stale databases rebuild from Markdown;
- FTS failure falls back without changing authoritative files;
- WAL and non-WAL configurations are exercised for desktop and shared-volume
  deployment modes.

### Writes and permissions

- high-value grounded candidates pass while medium/low, duplicate, secret-bearing,
  ungrounded, and unsafe-path candidates are discarded;
- conflicts are recorded rather than silently overwritten;
- normal-mode model proposals, explicit user requests, allow-once, session-wide
  allow, decline, and Approval-delivery failure follow the specified behavior;
- YOLO writes skip the popup but still run value/safety checks and audit;
- declining leaves no candidate or hidden review record;
- `ingest` accepts current permitted source content and rejects directories,
  archives, arbitrary paths, and credentials;
- ordinary filesystem tools cannot mutate the global wiki.

### Concurrency and recovery

- concurrent sessions serialize writers while readers observe committed versions;
- revision changes between proposal and lock acquisition force revalidation;
- process interruption is injected at every journal/temporary/replace/log/revision
  boundary and recovery reaches one complete state;
- FTS commit failure leaves Markdown intact and rebuildable;
- lock timeout, full disk, and read-only mounts produce actionable errors;
- `index.md`, `log.md`, page revisions, and global revision remain consistent.

### Authorization and deployment

- an arbitrary session UUID cannot read another workspace's legacy Knowledge data;
- global Wiki lookup uses the configured local-user scope;
- Docker workers mounted to the same Markdown root share knowledge;
- unsupported shared-filesystem WAL configurations use a process-local/disposable
  cache or bounded Markdown fallback;
- future namespace fixtures demonstrate that `users/default/wiki` can be migrated
  to a user-specific namespace without changing page links or workspace UUIDs.

## References

- [Karpathy, LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
- [SQLite FTS5 Extension](https://www.sqlite.org/fts5.html)
- [SQLite Write-Ahead Logging](https://www.sqlite.org/wal.html)
- [SQLite Atomic Commit](https://www.sqlite.org/atomiccommit.html)
- [Python `os.replace`](https://docs.python.org/3/library/os.html#os.replace)
