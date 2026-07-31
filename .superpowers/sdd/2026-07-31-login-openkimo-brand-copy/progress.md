# SDD ledger — plan: docs/superpowers/plans/2026-07-31-login-openkimo-brand-copy.md

## Release candidate result

- Outer design and plan: `ff4fe52`, `2a99e29`.
- Child product change: `8f14d7c2` (`fix: brand login subtitle as OpenKimo`).
- Outer product contract and formatting: `b3a4d48`, `bbae595`.
- Release changelog: `be64b5d`; the candidate is based on the existing `v0.1.25` tag and
  documents the login-brand change under `v0.1.26`.
- CI review remediation: `451bbc2` scopes Python lint/format to
  `src/kimi_cli/wiki tests/wiki` and runs outer tests from the outer repository root.

## Review and verification

- The release-candidate review identified two Important CI integration issues: unscoped Python
  lint/format and outer tests launched through the child working directory. Both are covered by
  `tests/test_ci_workflow.py` and fixed in `451bbc2`.
- TDD RED: the new CI contract failed twice against the original workflow, once for each issue.
- TDD GREEN: `kimi-cli/.venv/bin/python -m pytest tests/test_ci_workflow.py -q` — `2 passed`.
- Workflow-equivalent lint: `uv run ruff check src/kimi_cli/wiki tests/wiki` — passed.
- Workflow-equivalent format: `uv run ruff format --check src/kimi_cli/wiki tests/wiki` —
  `29 files already formatted`.
- Workflow-equivalent outer suite: `kimi-cli/.venv/bin/python -m pytest tests -v --tb=short` —
  `91 passed`.
- Pyright, frontend, backend dependency installation, and Docker workflow logic were left intact.

## Historical source reference

The earlier ledger named `623412f..43e74f3` and child `9cf67ee7`. Those hashes describe the
original isolated source work and are not the current release-candidate result; the reachable
candidate commits above are authoritative.
