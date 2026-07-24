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

- [ ] Task 1 — Generic Packaged Wiki Skeleton and User-Level Path
- [ ] Task 2 — Page Schema, Logical Paths, and Safety
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
