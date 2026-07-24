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

## Cross-platform re-review probes

9. Simulated Windows final verification allows a normal one-link workspace file
   for both WriteFile and StrReplaceFile, while direct Wiki paths, symlink aliases,
   and hardlinks are rejected.
10. Simulated non-local KAOS allows ordinary remote file operations. It rejects a
    path that explicitly equals the managed local root (or any future
    `wiki_remote_roots` mapping) without returning the local root in tool output.

Windows now resolves the final leaf and parent, rejects a symlink or link count
other than one, compares the lstat and opened-handle identities, then writes via
the opened descriptor. Non-local KAOS never performs a local `Path.resolve` on a
remote target: only explicitly configured textual root mappings are blocked, and
ordinary remote writes continue through KAOS.

- Cross-platform verification: focused `51 passed, 1 warning`; all `tests/wiki`
  plus `tests/tools` `665 passed, 1 warning`; Ruff check/format, Pyright (0
  errors), and `git diff --check` passed.
