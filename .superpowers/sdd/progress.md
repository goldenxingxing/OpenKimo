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
- [ ] Task 3 — Idempotent Initialization and Versioned Metadata
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
