# Task 10 Implementation Report

## Implemented

- Every `remember` and `ingest` write now prepares and passes the existing
  deterministic high-value/grounding gate before any permission decision.
- Normal mode requests the existing action-scoped Approval permission
  `wiki.write`. Approval waits outside the Wiki writer lock; accepted changes
  commit through the existing revision-checked transaction and safe independent
  rebase path.
- Added compact `WikiApprovalBlock` metadata containing only the summary,
  bounded logical page paths, portable workspace/session identifiers, and
  collapsed path-safe details. The Web approval keeps the existing three wire
  responses with Wiki-specific labels: allow once, always allow this session,
  and decline.
- Decline, cancellation, and approval-delivery failure commit nothing and leave
  no Wiki transaction pending.
- YOLO skips the popup only after the same strict candidate gate. AFK continues
  through the existing Approval flow; explicit normal-mode `remember` calls
  still require approval.
- Real user turns provide ephemeral, hash-only trusted provenance to the Wiki
  tool. Synthetic/internal turns fail closed, and trusted steer input extends
  only the active turn. The dynamic tool description exposes portable UUID and
  SHA-256 instructions, never machine paths or raw chat text.
- No session-end archive or automatic end hook was added. `soul/approval.py`
  required no change because its existing once/session/reject semantics already
  provide the required action-scoped behavior.

## TDD evidence

- Initial backend RED failed collection because `WikiApprovalBlock` did not
  exist.
- Initial Web contract RED produced two failures because Wiki display handling
  and locale keys were absent.
- Follow-up RED probes reproduced missing trusted-turn context, unbounded
  approval page metadata, missing Wiki-specific action labels, missing steer
  provenance extension, and absent portable provenance instructions.
- The focused Task 10 backend tests pass `14 passed`; the root UI contract tests
  pass as part of the complete root suite.

## Final verification

- Related Wiki/tool/approval/wire/runtime/Web Python suite:
  `789 passed, 2 skipped, 2 warnings`.
- Superproject Python suite: `75 passed, 2 warnings`.
- Ruff check and format check passed; focused Pyright passed with `0 errors`.
- Web `typecheck`, Biome lint, unit tests (`3 passed`), and production build
  passed. The build emitted only the existing Node deprecation and large-chunk
  notices.
- `git diff --check` passed in both the superproject and submodule.

The two Python warnings are the existing Loguru Python 3.14 deprecation
warnings. A separate full `tests/core` audit retained the known branch baseline:
`42 failed, 880 passed, 3 warnings, 2 errors`, in the same stale snapshot/helper
categories already recorded by Task 9; no Task 10-focused failure was present.

## Self-review

Approval is requested only after immutable preparation and never while holding
the writer lock. Approval metadata is bounded to 20 logical page paths, omits
raw transcripts and absolute paths, and uses the existing transport rather than
creating a parallel permission mechanism.
