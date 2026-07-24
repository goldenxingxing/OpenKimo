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
