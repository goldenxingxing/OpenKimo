# Task 8 review-fix report

## Root cause

The original managed-root check ran before approval, then `WriteFile` and
`StrReplaceFile` invoked KAOS pathname writes after approval. A concurrent actor
could replace that pathname with a Wiki symlink or hardlink during the approval
window. The Wiki tool also derived durable-admission facts from model-provided
candidate data and assumed that every session ID was a UUID.

## TDD probes

1. Atomic `os.replace` after approval changes a normal WriteFile target into a
   symlink to `index.md`: rejected; `index.md` remains unchanged.
2. Atomic `os.replace` after approval changes a normal WriteFile target into a
   hardlink to `index.md`: rejected due to the final descriptor's link count;
   `index.md` remains unchanged.
3. The equivalent symlink and hardlink/rename probes for StrReplaceFile:
   rejected with unchanged `index.md`.
4. A workspace ingest whose registered workspace UUID is absent from the
   runtime allowset: rejected before registry resolution.
5. A candidate marked `high` by the model while trusted runtime evidence says
   it is not high-value: rejected.
6. A named/non-UUID session ID with an independent trusted provenance UUID:
   accepted for preparation without parsing the session name.
7. A `WikiConflictError` containing a private path: converted to a safe retry
   response without exposing the path.
8. An arbitrary `OSError` containing a private path: converted to a generic
   safe error without exposing the path.

## Implementation contract

- `write_verified_text` is now the final mutation boundary. On local KAOS it
  opens the final parent with no-follow semantics, opens the final leaf via that
  descriptor with `O_NOFOLLOW`, verifies the leaf is one regular inode with a
  link count of one, and writes through the descriptor. Unsupported remote or
  Windows no-follow conditions fail closed whenever a Wiki manager is active.
- `WikiToolContext` is a trusted runtime-only contract for Task 9 wiring:
  provenance UUID, current-turn conversation hashes, allowed workspace UUIDs,
  and value/grounding facts. The model schema exposes none of these fields.
- `Params.instructions` was removed because it had no implemented controlled
  semantics.

## Verification

- Focused tool tests: `47 passed, 1 warning`.
- Wiki and tool suite: `661 passed, 1 warning`.
- Ruff check/format, Pyright, and `git diff --check`: passed.
