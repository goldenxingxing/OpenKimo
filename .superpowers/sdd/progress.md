# Global User Wiki — SDD Execution Ledger

## Execution identity

- Base commit: `b49643b9046631e32eaca5e552257503fbf04ad1`
- Branch: `codex/global-user-wiki`
- Worktree: `/Users/qunwei/Documents/local_agent_work/OpenKimo/.worktrees/global-user-wiki`
- Plan: `docs/superpowers/plans/2026-07-24-global-user-wiki.md`
- Design: `docs/superpowers/specs/2026-07-24-global-user-wiki-design.md`
- Protected user change: `packaging/venvstacks.resolved.toml` (main worktree only;
  never modify, stage, or commit it)

## Preflight

- `.worktrees/` is ignored by the repository (`.gitignore:117`).
- The planned submodule revision `fdef5b4a` is present locally but unavailable from
  the configured public submodule remote. The isolated worktree therefore uses a
  shared local clone checked out at that exact revision; its content and commit ID
  match the superproject gitlink.
- A dedicated `kimi-cli/.venv` was created with `uv sync --all-groups` in the
  isolated worktree.
- No Critical design/plan contradiction found.
- Important implementation check: Task 1 must verify that Markdown/JSON templates
  under `src/kimi_cli/wiki/templates/` are included in every Python/package build
  artifact (not merely present in a source checkout). If the current build metadata
  does not include them automatically, update package-data configuration and add a
  wheel/staging assertion within that task.

## Baseline verification

| Command | Result |
| --- | --- |
| `kimi-cli/.venv/bin/python -m pytest tests -q` (superproject) | 70 passed, 2 warnings |
| `cd kimi-cli && uv run pytest tests/core/test_skill.py tests/skill tests/tools tests/web -q` | 447 passed, 2 skipped, 3 warnings |
| `cd kimi-cli && uv run pytest tests -q` | known baseline collection failure: duplicate bare module name `test_manager` in `tests/background` and `tests/skill` |
| `cd kimi-cli && uv run pytest tests -q --import-mode=importlib` | known baseline collection failure: `tests_e2e` import not resolvable for five `tests/e2e` tests |

The two full-suite collection failures pre-exist this feature and are not changed by
this branch. Focused verification must retain the passing command above and later
report any change to these failures separately.

## Plan status

- [x] Task 1 — Generic Packaged Wiki Skeleton and User-Level Path
- [x] Task 2 — Page Schema, Logical Paths, and Safety
- [x] Task 3 — Idempotent Initialization and Versioned Metadata
- [ ] Task 4 — Stable Workspace Registry and Portable Provenance
- [ ] Task 5 — Disposable FTS5 Search Cache and Markdown Fallback
- [ ] Task 6 — Cross-Process Lock, Durable Transaction, and Recovery
- [ ] Task 7 — WikiManager, Value Gate, Merge, Audit, and Lint
- [ ] Task 8 — Dedicated Wiki Tool and Managed-Root File Guard
- [ ] Task 9 — Bounded Session Awareness and Shared Runtime Wiring
- [ ] Task 10 — Compact Approval and YOLO Write Policy
- [ ] Task 11 — Knowledge API Authorization and Global-Scope Migration
- [ ] Task 12 — Docker/KAOS, Packaging, No-End-Hook, and Full Verification
- [ ] Final integration review and completion workflow

## Task records

### Task 1 — Generic Packaged Wiki Skeleton and User-Level Path

- Added the shared `users/default/wiki` path resolver, schema version constant,
  generic Markdown/JSON templates, and platform-specific `AppPaths.wiki_dir`.
- Desktop servers now export `OPENKIMO_APP_DATA_DIR` and `OPENKIMO_WIKI_ROOT`
  after loading user configuration, so legacy `.env` values cannot override them.
- Added explicit `uv_build` package inclusion and PyInstaller collection for
  `wiki/templates/**`; tests build a wheel and inspect the PyInstaller data set.
- TDD red evidence: `kimi-cli/.venv/bin/python -m pytest
  kimi-cli/tests/wiki/test_paths.py kimi-cli/tests/wiki/test_initialization.py
  tests/test_work_directory_settings.py tests/test_skill_packaging.py -q`
  initially produced 7 expected failures for the absent Wiki module/assets,
  desktop fields, and package configuration.
- Green verification: the same focused test command passed `17 passed, 1 warning`.
  `cd kimi-cli && uv run ruff check src/kimi_cli/wiki
  src/kimi_cli/utils/pyinstaller.py tests/wiki` and the matching
  `ruff format --check` both passed. `git diff --check` passed.
- The wheel build emitted only the existing uv-build range warning; the wheel
  contains all five template assets. No protected packaging resolution file was
  changed.

### Task 2 — Page Schema, Logical Paths, and Safety

- Added strict Pydantic page, change, candidate, and portable provenance models;
  frontmatter accepts exactly the specified fields and requires timezone-aware,
  monotonic timestamps and positive revisions.
- Added canonical two-segment category paths, UTF-8/Chinese slugs, safe Markdown
  parsing/rendering, SHA-256 content hashes, WikiLink validation, and canonical
  filesystem resolution that rejects intermediate or final symlink escapes.
- Provenance rejects absolute/traversing/sensitive paths, credential-bearing web
  URLs, unknown source kinds, secret query parameters, and secret/absolute-path
  content in pages. The authority remains Markdown; no cache or write workflow was
  introduced ahead of later tasks.
- TDD red evidence: `cd kimi-cli && uv run pytest tests/wiki/test_schema.py
  tests/wiki/test_path_safety.py -q` initially failed collection because the
  models/schema modules did not exist.
- Green verification: the focused command passed `25 passed, 1 warning`; full
  Wiki tests passed `31 passed, 1 warning`; Ruff check/format, Pyright (0 errors),
  and `git diff --check` passed. The warning is the existing Loguru Python 3.14
  deprecation warning.
- Review follow-up: fixed two Important findings without expanding into later
  tasks. Portable provenance now explicitly rejects `PureWindowsPath` drive/UNC
  forms (including slash and backslash variants); page content detects any local
  POSIX absolute path, Windows drive path, or UNC path while preserving ordinary
  HTTPS Markdown URLs and prose such as `and/or`.
- Review follow-up: query parameter names are decoded by `parse_qsl`, case-folded,
  normalized across separators, and checked for API-key, credential, cookie,
  session, signature, auth, authorization, token, and password families,
  including `apiKey`, percent-encoded names, and `X-Amz-Signature`.
- Re-verification after the review fix: focused schema/path tests passed `43
  passed, 1 warning`; all Wiki tests passed `49 passed, 1 warning`; Ruff
  check/format, Pyright (0 errors), and `git diff --check` passed.
- Second review follow-up: provenance now inspects both URL query and fragment
  parameter components after percent decoding and separator/case normalization.
  It rejects OAuth, cookie/session, user-password, API-key, signature, and cloud
  signing families (including `client_secret`, `refresh_token`, `id_token`,
  `auth_token`, `x-goog-signature`, and `sig`) while retaining ordinary parameters.
- Second review follow-up: page-body guarding now rejects `file:` URIs, known
  machine-specific POSIX roots, Windows drive paths, and UNC paths, but explicitly
  permits `./docs/intro`, Markdown `/docs/intro`, `/api/v1/items`, and HTTPS URLs.
  Re-verification passed focused `56 passed, 1 warning` and all Wiki tests `62
  passed, 1 warning`, with Ruff check/format, Pyright (0 errors), and
  `git diff --check` passing.
- Third review follow-up: body-path handling now uses explicit syntax context
  rather than a directory allowlist. A Markdown root-relative link target and an
  `/api` endpoint token are web syntax and are excluded from local-path scanning;
  every other bare POSIX-root token (for example `/data`, `/srv`, or
  `/workspace`) is rejected. This is the documented decision for inherently
  ambiguous slash-prefixed text.
- Third review follow-up: current-drive-rooted Windows paths (`\\Windows...` and
  `\\Users...`), drive paths, UNC paths, and only true `file:/`, `file://`, or
  `file:\\` URIs are rejected. Bare `file:` prose and HTTPS URL path text such as
  `/file:/manual` remain valid.
- Third review follow-up: sensitive URL parameter matching is exact after percent
  decoding/separator normalization, with bounded camel-case components only for
  cloud-signing names. This preserves non-secret names such as `sessionTitle`,
  `sessionization`, `tokenizer`, `credentialing`, and `apikeyword` while rejecting
  the defined secret aliases. Focused verification passed `69 passed, 1 warning`;
  all Wiki tests passed `77 passed, 1 warning`; Ruff check/format, Pyright (0
  errors), and `git diff --check` passed.
- Final review follow-up: centralized decoded query/fragment alias and cloud-name
  normalization in `kimi_cli.wiki.models`, so source provenance and body URLs use
  the same checks. Added exact aliases for `session_token`, `secret_key`,
  `private_key`, `user_token`, and X-Amz/X-Goog signing names while preserving
  harmless prefix words.
- Final review follow-up: `/api` masking now validates decoded path segments before
  masking, so literal or percent-encoded `.` / `..` segments are rejected. Body
  validation also rejects credential-bearing HTTP/Markdown-link query or fragment
  parameters plus exact secret assignments and Cookie/Authorization-style headers;
  ordinary discussion text and a harmless `topic=api_key` value remain valid.
- Final review verification: the new TDD cases first produced 18 focused failures;
  after implementation, focused tests passed `90 passed, 1 warning` and all Wiki
  tests passed `96 passed, 1 warning`. Ruff check/format, Pyright (0 errors), and
  `git diff --check` passed.
- Final credential follow-up: the shared URL credential helper now includes URL
  userinfo as well as sensitive query/fragment parameters, and is used for both
  source provenance and raw/Markdown body URLs. Body secret assignment/header
  matching now recognizes standard bulleted and inline forms while excluding
  URL-query syntax, so ordinary discussion and `topic=api_key` values remain
  allowed.
- Final credential verification: the new cases first produced 5 focused failures;
  focused tests then passed `96 passed, 1 warning` and all Wiki tests passed `102
  passed, 1 warning`. Ruff check/format, Pyright (0 errors), and
  `git diff --check` passed.

### Task 3 — Idempotent Initialization and Versioned Metadata

- Added `WikiLayout`, `layout_for`, `ensure_wiki`, and `UnsupportedWikiSchema`.
  Initialization creates the six empty category directories, four user-owned
  Markdown special files, `.openkimo/journal`, `.openkimo/locks`, a manifest,
  and the global revision file without replacing existing Markdown.
- The managed root is canonicalized; managed children must be regular files or
  real directories contained beneath that root. Existing symlinks and path-type
  collisions are rejected before they can redirect writes outside the Wiki.
- The manifest has an exact validated shape and default namespace. Future schema
  versions fail closed before special Markdown is created; lower versions can
  advance only through an explicit one-version migration. A migration atomically
  replaces only the software-owned manifest, never user Markdown.
- TDD red evidence: `cd kimi-cli && uv run pytest
  tests/wiki/test_initialization.py -q` initially failed collection because
  `kimi_cli.wiki.initialize` did not exist. The explicit migration persistence
  test then failed until manifest replacement was implemented.
- Green verification: focused initialization tests passed `6 passed, 1 warning`;
  all Wiki tests passed `105 passed, 1 warning`; Ruff check and format checks
  passed; Pyright on `src/kimi_cli/wiki` passed with `0 errors`; and
  `git diff --check` passed. The warning is the existing Loguru Python 3.14
  deprecation warning.
- `pyright src/kimi_cli/wiki tests/wiki` still reports six pre-existing type
  errors in Task 2 test fixtures (`test_path_safety.py` string UUID values and
  `test_schema.py` string `HttpUrl` values). Task 3's source and its own test
  file type-check cleanly; no production behavior was weakened to mask those
  fixture-only errors.

### Task 4 — Stable Workspace Registry and Portable Provenance

- Added `WorkspaceRegistry` backed by `.openkimo/workspaces.json`, with an
  independent cross-process sidecar lock and atomic JSON replacement. It stores
  canonical absolute workspace roots only in the registry, updates a supplied
  UUID when a workspace moves, and keeps user-authored Wiki Markdown untouched.
- `relative_source` emits only workspace UUID, POSIX-relative source path, and
  content hash. Resolution rejects unknown, missing, absolute, traversing,
  Windows-style, and symlink-escaping sources; it only returns an existing
  regular file under the currently registered root.
- Shared relative-path validation now lives in `wiki.models`, so `SourceRef`,
  `CurrentSource`, and registry defense-in-depth use the same portable-path
  rule.
- TDD red evidence: `cd kimi-cli && uv run pytest tests/wiki/test_workspaces.py
  -q` failed collection as expected because `kimi_cli.wiki.workspaces` did not
  exist.
- Green verification: focused registry tests passed `10 passed, 1 warning`;
  all Wiki tests passed `115 passed, 1 warning`; Ruff check/format, Pyright on
  the Task 4 source/models/test (0 errors), and `git diff --check` passed. The
  warning is the existing Loguru Python 3.14 deprecation warning.
- Awaiting independent task review before the controller marks Task 4 complete.
- Review follow-up: each workspace now receives a tiny `.openkimo-workspace.json`
  identity marker containing only schema version and UUID. It is created with
  exclusive creation plus `fsync`, rejects symlinks/non-regular files, malformed
  or future-version payloads, and refuses a copied marker that would hijack a
  still-live workspace identity. A fresh registry instance recovers the UUID
  after a directory move from this marker, while the absolute path remains only
  in the central registry.
- Review follow-up: `workspaces.json` now writes strict
  `{"schema_version":1,"workspaces":...}` data. The previous unversioned
  shape has one explicit atomic migration; unsupported versions fail closed and
  are not rewritten.
- Review-fix TDD: the new fresh-instance move and symlink-marker tests first
  failed (`2 failed, 9 passed`); registry-version tests then first failed (`3
  failed, 11 passed`); strict boolean/float marker-version tests first failed
  (`2 failed, 3 passed`). Re-verification passed focused `20 passed, 1 warning`
  and all Wiki tests `125 passed, 1 warning`, plus Ruff check/format, Pyright
  (0 errors), and `git diff --check`.

### Task 5 — Disposable FTS5 Search Cache and Markdown Fallback

- Added `WikiSearchIndex` and `SearchResult` with an authoritative-page input
  boundary: the SQLite database contains only derived logical path, content hash,
  revision, title, tags, summary, and body rows; Markdown is never modified.
- The cache detects FTS5 trigram availability for Chinese/mixed-language substring
  search. Short queries and installations without trigram first use deterministic
  title/tag matching, then bounded escaped `LIKE`; no FTS5 installation uses the
  same bounded fallback. Results include deterministic logical-path tie breaks,
  snippets, hashes, and revisions, with limits clamped to 1–20.
- `sync()` atomically removes deleted/changed rows and writes only changed hashes;
  `rebuild()` replaces the disposable cache from validated pages. A query error
  falls back to the current bounded Markdown page set.
- Cache opening honors explicit WAL configuration. Invalid SQLite bytes or an
  incompatible stale cache schema is quarantined to a temporary diagnostic name,
  replaced with a clean cache, and the diagnostic is removed after successful
  initialization; unrelated SQLite failures are not deleted.
- TDD red evidence: `cd kimi-cli && uv run pytest tests/wiki/test_search.py
  tests/wiki/test_search_recovery.py -q` first failed with seven errors/failures
  because `kimi_cli.wiki.search` did not exist.
- Green verification: focused search tests passed `10 passed, 1 warning`; all
  Wiki tests passed `135 passed, 1 warning`; Ruff check/format passed; Pyright on
  `src/kimi_cli/wiki/search.py` reported `0 errors`; and `git diff --check`
  passed. The warning is the existing Loguru Python 3.14 deprecation warning.
- Implementation commit: submodule `45d76eb feat: index global wiki with fts5`.
  Awaiting independent Task 5 review before the controller marks this task
  complete.
- Review follow-up: `wal=False` now actively executes `PRAGMA journal_mode=DELETE`
  when reopening a cache, rather than inheriting the persistent WAL mode selected
  by a previous open. The mode switch happens during cache setup; a locked or
  otherwise unrelated SQLite error still propagates and is not treated as corrupt.
- Review-fix TDD: reopening one cache first with `wal=True`, then `wal=False`,
  initially failed because SQLite remained in WAL mode. Re-verification passed
  focused `10 passed, 1 warning` and all Wiki tests `135 passed, 1 warning`, with
  Ruff check/format, Pyright (0 errors), and `git diff --check` passing.
- Review-fix commit: submodule `282f51e fix: reset wiki cache journal mode`.

### Task 6 — Cross-Process Lock, Durable Transaction, and Recovery

- Added deadline-based `WikiLock` shared/exclusive contexts using `fcntl.flock`
  on POSIX and conservative exclusive byte-range locking with `msvcrt` on
  Windows. Lock files are opened as verified regular files, symlinks are
  rejected, and timeout failures are retryable `WikiBusyError` exceptions.
- Shared Wiki reads use `wiki_read_lock`: recovery runs while the lock is
  exclusive and the same descriptor is atomically downgraded to shared before
  any Markdown is exposed. A valid partial journal whose recovery hits an I/O
  failure blocks the read rather than exposing a half-commit; an unreadable
  journal sets a persistent write quarantine while retaining read-only access.
- `WikiTransaction.prepare` captures page/special-file hashes under a stable
  shared view. `commit` acquires the global writer lock, recovers older work,
  revalidates page hashes/revisions, special-file hashes, and global revision,
  then durably journals old/new hashes plus rollback artifacts.
- Commit order is deterministic: sorted content pages, `index.md`, `log.md`,
  `.openkimo/revision`, then the committed journal marker. All artifacts and
  sibling replacements are flushed with `fsync`; parent directory metadata is
  flushed on POSIX; recorded targets and artifact names are strict relative
  allowlisted paths.
- Recovery discards an untouched prepared transaction, rolls a hash-consistent
  partial transaction forward, restores durable backups when forward completion
  is impossible, and reports committed Markdown as requiring FTS rebuild.
  Authoritative journal completion is independent of the disposable search-cache
  rebuild protocol described in the review follow-up below.
- TDD red evidence: `cd kimi-cli && uv run pytest tests/wiki/test_locking.py
  tests/wiki/test_transaction.py tests/wiki/test_recovery.py -q` initially
  failed collection with three expected missing-module errors for
  `wiki.locking` and `wiki.transaction`.
- Focused verification passed `28 passed, 1 warning`, including two-process
  writer serialization, a reader blocked at an injected mid-commit boundary,
  five ordered failpoints, roll-forward/rollback, malicious journal paths,
  write quarantine, and FTS rebuild acknowledgement/retention.
- All Wiki tests passed `163 passed, 1 warning`. Ruff check/format passed for all
  Wiki source/tests; Pyright on `src/kimi_cli/wiki` reported `0 errors`; staged
  and unstaged diff checks passed. The warning is the existing Loguru Python
  3.14 deprecation warning.
- Implementation commit: submodule `652a6b8 feat: make wiki commits
  recoverable`. Awaiting independent Task 6 review before the controller marks
  this task complete.
- Task 7 integration note: `WikiTransaction.commit()` owns the writer lock and
  revalidation boundary. `WikiManager` must not wrap it in a second exclusive
  lock; approval remains outside `commit`, and prepared changes are revalidated
  inside it.
- Task 6 independent review returned `CHANGES_REQUIRED` with two Critical and
  seven Important findings. TDD reproductions first produced `16 failed, 29
  passed`; every Critical/Important finding was fixed without entering Task 7.
- Critical recovery-protocol follow-up: committed authoritative journals now
  create/update a separate `.openkimo/search.invalid` revision marker and clean
  themselves independently of SQLite. Search rebuild failure leaves only that
  marker, never a write quarantine; consecutive commits monotonically advance
  it, restart recovery remains writable, and `acknowledge_reindex` clears it
  only when the rebuilt revision still equals the current required revision.
- Critical unknown-content follow-up: a current target hash outside the
  journal's recorded old/new set is never overwritten or rolled back. Recovery
  preserves the complete on-disk state and persistently quarantines writes for
  operator review.
  Rollback is allowed only when every current hash is known and every required
  old artifact was hash-validated.
- Artifact recovery now opens each durable artifact once, verifies its hash and
  page/special-file semantics, and passes those exact bytes to `os.replace`.
  Prepared records validate logical page paths, old/new page revisions, strict
  page schema, expected page revisions, UTF-8 special Markdown, and exact old/new
  global revision artifact bytes. A completed authoritative commit no longer
  depends on disposable journal artifact copies.
- `expected_global_revision` and rebuilt revisions are strict non-boolean
  integers. Lock timeouts must be finite non-negative built-in numbers, rejecting
  booleans, NaN, and infinities. The Windows contract explicitly exposes that
  shared locks conservatively serialize as exclusive; platform-conditioned tests
  cover this, failed acquisition no longer attempts unlock, and actual unlock
  errors propagate.
- Reindex acknowledgement has explicit success/required-revision results.
  Marker read, delete, and directory-`fsync` failures propagate; a stale rebuild
  cannot acknowledge a newer commit. Failpoints now cover journal directory
  flushes, artifact create/file-`fsync`/directory-`fsync`, prepared and committed
  record replace/flush, every ordered target replace, cache-marker replace/flush,
  rollback replace/remove, journal cleanup/delete/flush, and reindex
  acknowledgement delete/flush.
- Review-fix verification passed focused `61 passed, 1 warning` and all Wiki
  tests `196 passed, 1 warning`. Ruff check/format passed on all Wiki
  source/tests; Pyright passed both all Wiki source and the focused Task 6
  source/tests with `0 errors`; `git diff --check` passed. The warning remains
  the existing Loguru Python 3.14 deprecation warning.
- Review-fix commit: submodule `55bbb9f fix: harden wiki transaction recovery`.
  Awaiting independent Task 6 re-review before the controller marks this task
  complete.
- Task 6 re-review retained one Critical/Important cleanup gate and one Important
  fault-injection coverage gap. A committed journal whose cleanup fails before
  deletion now reports `post_commit_cleanup_pending` to readers but rejects both
  `prepare` and `commit` with `WikiRecoveryRequired(retryable=True)` and an
  actionable recovery instruction. Once a later recovery durably removes the
  journal, writes resume without setting quarantine.
- The second review RED suite reproduced the missing behavior with 27 failures
  across the pending-cleanup gate and previously absent sibling-temp failpoints.
  `_durable_replace` now exposes actual post-create, post-write, post-file-`fsync`,
  pre-`os.replace`, post-replace, and post-directory-`fsync` seams. Journal
  cleanup exposes a pre-delete seam in addition to post-delete and
  post-directory-`fsync`.
- Real injected-failure recovery tests now exercise every page/index/log/revision
  target boundary, prepared/committed record boundary, cache-marker and journal
  cleanup boundary, rollback replace/remove boundary, plus the existing
  artifact, acknowledgement, and directory-flush boundaries. Tests assert a
  complete before/after authority state, retryability, cache invalidation
  revision, and absence of false quarantine rather than merely checking
  failpoint names.
- Second review-fix verification passed focused `116 passed, 1 warning` and all
  Wiki tests `251 passed, 1 warning`. Ruff check/format, Pyright on all Wiki
  source and the changed Task 6 source/tests, and `git diff --check` passed.
- Second review-fix commit: submodule `08598b5 fix: gate writes on pending wiki
  cleanup`. Awaiting independent Task 6 re-review.

### Task 7 — WikiManager, Value Gate, Merge, Audit, and Lint

- Added the application-level `WikiManager` boundary for shared initialization,
  search/read, high-value candidate preparation, explicit commit, current-source
  ingest, and read-only lint.
- The permission interval is lock-free: preparation returns a complete
  revision-checked transaction, and commit delegates directly to Task 6 without
  wrapping a second exclusive lock.
- Cache refresh captures a complete authoritative Markdown snapshot at revision R
  under one read lock, releases that lock before SQLite callbacks, syncs the full
  snapshot, and acknowledges exactly R; stale acknowledgements remain safe.
- The deterministic gate rejects low/medium, one-turn, unstable, ungrounded,
  sensitive, and duplicate proposals in memory. Mixed candidates discard duplicate
  pages while retaining novel pages; same-page provenance improvements merge.
- Conflict updates preserve the existing sourced position plus an explicit
  additional sourced position, increment page revision, rebuild the categorized
  index, and append a delimiter-safe audit record.
- Lint reports malformed/non-UTF-8 pages, dead links, orphans, duplicate
  hashes/claims, conflict markers, and missing provenance without modifying
  content.
- TDD RED evidence and the complete implementation report are recorded in
  `.superpowers/sdd/task-7-report.md`.
- Fresh verification passed focused `30 passed`, all Wiki `281 passed`, and
  approval regressions `29 passed`; Ruff check/format, Pyright (0 errors), and
  `git diff --check` passed. The sole warning is the existing Loguru Python 3.14
  deprecation warning.
- Implementation commit: submodule `e547b315 feat: add global wiki manager`.
  Awaiting independent Task 7 review before the controller marks this task
  complete.
- Independent Task 7 review returned CHANGES_REQUIRED. All Critical and Important
  findings were reproduced with focused RED tests, then fixed in submodule commit
  `22ecd4da fix: harden global wiki manager`.
- Review follow-up added complete frontmatter/source safety validation,
  revision-CAS and cross-thread-safe SQLite access, authoritative Markdown search
  fallback, post-authority cache error containment, safe independent-change
  rebase with immutable approved input, malformed logical-page isolation,
  credential-URL ingest validation, source-associated conflict blocks, and
  registry-backed provenance lint.
- Review-fix verification passed focused `56 passed`, all Wiki `301 passed`, and
  approval regressions `29 passed`; Ruff check/format, Pyright (0 errors), and
  `git diff --check` passed. Awaiting independent Task 7 re-review.

### Task 8 — Dedicated Wiki Tool and Managed-Root File Guard

- Added the default-agent `Wiki` tool with controlled `search`, `read`, `lint`,
  `remember`, and `ingest` operations. Read-only operations return only logical
  paths and validated page/report data. Remember and ingest prepare structured,
  high-value candidates without committing; Task 10 supplies the approval and
  commit boundary.
- Ingest accepts only `CurrentSource` inline content or a registered
  workspace-relative file. Raw paths and URLs are absent from the tool schema;
  archive suffixes, sensitive inline content, ungrounded sources, and unavailable
  Wiki sessions fail closed. Tool errors do not echo underlying machine paths.
- Added one shared pre-approval guard to WriteFile and StrReplaceFile. It rejects
  every local descendant and resolved symlink alias of `Runtime.wiki`'s managed
  root, directing mutation through the Wiki tool instead.
- TDD RED evidence: the new focused suite initially failed collection because
  `kimi_cli.tools.wiki` did not exist. The later no-machine-path test failed as
  expected before generic error handling was added.
- Verification passed focused `39 passed, 1 warning` and all `tests/wiki`
  plus `tests/tools` `653 passed, 1 warning`; Ruff check/format, Pyright (0
  errors), and `git diff --check` passed. The warning is the existing Loguru
  Python 3.14 deprecation warning.
- Implementation commit: submodule `b1a1d70f feat: expose controlled wiki tool`.
  Awaiting independent Task 8 review before the controller marks this task complete.
- Review-fix root cause: the initial managed-root check occurred before Approval,
  while the final KAOS pathname write remained vulnerable to target replacement.
  `write_verified_text` is now the final mutation boundary. Local writes open the
  verified parent and leaf with no-follow file descriptors, reject non-regular or
  multiply-linked inodes, and write through the final descriptor; remote and
  Windows paths fail closed while a Wiki manager is active if this proof cannot
  be made.
- Review-fix admission contract: `WikiToolContext` carries trusted runtime
  provenance UUID, turn conversation hashes, allowed workspace UUIDs, and
  high-value/stability/grounding facts. Candidate fields cannot self-certify any
  of these. Named sessions use that independent provenance UUID rather than
  parsing `Session.id`; Task 9 owns its runtime wiring. The unused `instructions`
  parameter was removed.
- Review-fix TDD probes cover atomic post-approval symlink and hardlink
  `os.replace` retargeting for both file tools, missing workspace allowset,
  self-certified high-value rejection, named-session provenance, safe domain
  conflict retry output, and no-path leakage. The detailed probes are recorded
  in `.superpowers/sdd/task-8-review-fix-report.md`.
- Review-fix verification passed focused `47 passed, 1 warning` and all
  `tests/wiki` plus `tests/tools` `661 passed, 1 warning`; Ruff check/format,
  Pyright (0 errors), and `git diff --check` passed. Awaiting Task 8 re-review.
- Cross-platform re-review follow-up: Windows uses final target/parent resolution,
  symlink and link-count rejection, lstat/opened-handle identity comparison, and
  descriptor writes, so ordinary one-link workspace files remain writable while
  managed paths, aliases, and hardlinks are rejected. Non-local KAOS no longer
  applies local filesystem resolution or blocks every normal write; it blocks only
  an explicitly expressible managed-root path (the local root or trusted future
  `wiki_remote_roots` mapping), without exposing a local path in output.
- Cross-platform probes cover simulated Windows and non-local KAOS WriteFile and
  StrReplaceFile ordinary writes plus managed-root rejection. Verification passed
  focused `51 passed, 1 warning` and all `tests/wiki` plus `tests/tools` `665
  passed, 1 warning`; Ruff check/format, Pyright (0 errors), and `git diff --check`
  passed. Awaiting Task 8 re-review.

### Task 9 — Bounded Session Awareness and Shared Runtime Wiring

- Added a UTF-8-safe compact index renderer with strict 8 KiB/80-entry defaults,
  whole-entry truncation, reserved marker space, and hinted-entry ranking.
- `Runtime.create` now initializes the configured shared Wiki without blocking
  session creation on failure. Local workspaces receive stable registry UUIDs;
  remote KAOS sessions retain shared Wiki search/read while leaving local
  workspace provenance unset.
- Runtime creates stable trusted session provenance and a fail-closed
  `WikiToolContext`; per-turn high-value, source, user-confirmation, and approval
  policy remain explicitly deferred to Task 10.
- Root and subagent runtimes share the same Wiki manager, workspace UUID, and
  trusted context references. The default prompt conditionally includes three
  concise guidance sentences and the bounded index.
- TDD RED failed during collection because `kimi_cli.wiki.context` did not
  exist. Final focused verification passed `703 passed, 2 skipped, 2 warnings`
  across Wiki, tools, Task 9/runtime-role tests, and web tests. Ruff
  check/format, Pyright (0 errors), and `git diff --check` passed.
- Implementation commit: submodule `d9db5cf8 feat: add global wiki session
  awareness`. Awaiting independent Task 9 review.
- Independent review follow-up added stable bounded prompt-block replacement for
  resumed and pre-upgrade sessions, atomic context persistence, one deduplicated
  localized fail-open Wiki warning, and thread-safe/idempotent root-owned Wiki
  lifecycle cleanup across CLI shutdown, Web worker exit, startup failures, and
  cancellation. Subagents never close the shared manager.
- Review RED probes reproduced missing prompt APIs, unclosed worker/runtime paths,
  and leaked partial initialization on cancellation. Final verification passed
  related `734 passed, 2 skipped, 2 warnings` plus Context `20 passed, 1 warning`;
  Ruff check/format, Pyright (0 errors), and `git diff --check` passed.
- Review-fix commit: submodule `8c0314c3 fix: harden global wiki session wiring`.
  Awaiting independent Task 9 re-review.
- Final Task 9 review follow-up precisely migrates the previous unmarked Wiki
  prompt format without deleting adjacent custom content, closes every
  successfully created CLI root runtime from the outermost owner `finally`
  (including SessionStart failure/cancel and all UI switches), and shields Wiki
  manager thread construction so cancellation waits for and closes the completed
  SQLite/WAL owner.
- Final RED probes reproduced stale legacy blocks, the missing CLI owner, and
  zero close calls in the constructor-cancel race. Verification passed related
  `745 passed, 2 skipped, 2 warnings`, Context `20 passed, 1 warning`, Ruff
  check/format, Pyright (0 errors), and `git diff --check`.
- Final review-fix commit: submodule `c478b4dc fix: close final wiki lifecycle
  gaps`. Awaiting final Task 9 review.
