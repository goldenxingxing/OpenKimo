# Task 9 Implementation Report

## Implemented

- Added UTF-8-safe compact Wiki index rendering with strict byte and entry
  limits, whole-entry truncation, a reserved marker, and hinted-entry ranking.
- Initialized the shared Wiki during `Runtime.create` without allowing Wiki
  failures to block session creation.
- Registered local workspaces for portable provenance; remote KAOS sessions
  retain shared Wiki search/read without incorrectly registering remote paths
  as local sources.
- Added stable trusted session provenance and a fail-closed `WikiToolContext`.
  Task 10 remains responsible for trusted per-turn write/approval facts.
- Shared the same Wiki, workspace UUID, and trusted context references with
  subagents.
- Added a conditional three-line Wiki guidance section and bounded index to the
  default system prompt.
- Fixed the Wiki tool's postponed constructor annotation so runtime dependency
  injection can instantiate it.

## TDD evidence

- RED: `uv run pytest tests/wiki/test_context.py tests/core/test_agent.py -q`
  failed during collection with
  `ModuleNotFoundError: No module named 'kimi_cli.wiki.context'`.
- GREEN: the focused suite passed `13 passed, 1 warning`.

## Final verification

- `uv run pytest tests/wiki tests/tools tests/core/test_agent.py
  tests/core/test_runtime_roles.py tests/core/test_runtime_afk_state.py tests/web
  -q`: `703 passed, 2 skipped, 2 warnings`.
- Focused Ruff check and format check passed.
- Focused Pyright passed with `0 errors`.
- `git diff --check` passed.

The warnings are the existing Loguru Python 3.14 deprecation warning. A separate
full `tests/core` run exposed 42 pre-existing branch failures, principally stale
Task 8/skill snapshots, the existing `InstallSkill` postponed constructor
annotation, and older test helpers missing `Runtime.user_memory_dir`; Task 9's
focused runtime, Wiki/tool, and web suites are green.

## Commit

- `d9db5cf8 feat: add global wiki session awareness`

## Self-review

The implementation remains within Task 9. It does not commit Wiki proposals,
alter approval policy, or add an end-of-session write/archive hook.

## Review follow-up

- Added stable managed-block markers around the Wiki prompt section. Resumed
  sessions now atomically replace the current block in persisted context,
  insert it into pre-upgrade prompts, collapse duplicates, and remove stale
  blocks when Wiki is unavailable while preserving all unmanaged prompt text.
- The complete marked Wiki block, including guidance and headings, is capped at
  8 KiB by UTF-8 bytes.
- Wiki initialization failure remains fail-open and now emits one deduplicated,
  localized, path-safe warning through the existing wire/shell notification
  channel. Detailed errors remain in logs.
- Made `WikiManager.close` thread-safe and idempotent. Only root runtimes close
  the shared manager; subagents borrow it. CLI shutdown, Web worker exit,
  post-runtime startup errors, telemetry failures, and cancellation during
  partial Wiki initialization all release SQLite/WAL resources.
- Review RED evidence:
  - the first review suite failed collection because managed-block APIs did not
    exist;
  - worker exit and post-runtime progress tests failed because close was never
    awaited;
  - partial-initialization cancellation failed because the manager remained
    open.
- Final verification:
  - related Wiki/tool/runtime/web suite: `734 passed, 2 skipped, 2 warnings`;
  - Context persistence suite: `20 passed, 1 warning`;
  - Ruff check/format, Pyright (`0 errors`), and `git diff --check` passed.

## Review-fix commit

- `8c0314c3 fix: harden global wiki session wiring`
