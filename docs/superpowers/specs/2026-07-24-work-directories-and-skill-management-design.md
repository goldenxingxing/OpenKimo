# Work Directories and Skill Management Design

## Goal

Make session storage predictable across macOS and Windows, remove path configuration
from desktop Settings, and provide one cross-work-directory skill system that users
and agents can manage safely from the Admin Panel.

## Work directory behavior

The New Session dialog remains the only place where a user chooses a session work
directory. Its first-run default is the platform Documents directory followed by
`OpenKimo`; later sessions default to the most recently used directory.

Creating a session ensures these directories exist below the selected work directory:

```text
<work-directory>/
├── session-data/
└── output/
```

No `skill` or `skills` directory is created inside a session work directory.

The macOS and Windows Settings interfaces remove their Paths sections. Existing
environment values remain readable for backward compatibility, but are no longer
editable in Settings and do not override a directory explicitly chosen for a new
session.

Existing sessions remain readable from their legacy storage locations. New sessions
use `<work-directory>/session-data`. Session workers receive the matching
`<work-directory>/output` path rather than a process-global output directory.

## Skill layers

Skills are resolved from two managed layers:

1. A read-only built-in layer stored at `kimi_cli/skills` inside the source tree and
   packaged application.
2. A writable user layer stored in the OpenKimo application-data directory under
   the singular directory name `skill`.

Built-in skills are not copied on first launch. The effective catalog is a merged
view. A writable skill with the same normalized name shadows its built-in
counterpart. Editing a built-in skill uses copy-on-write for that skill only.
Disabling or deleting a built-in skill records a tombstone in a state file; restoring
it removes the override and tombstone.

All sessions resolve these layers by canonical absolute paths. The selected work
directory never changes the managed skill locations. Existing project-local and
user-tool skill discovery remains supported at its current higher precedence.

## Admin Panel

The Admin Panel gains a Skill tab. It lists effective skills with name, description,
origin, state, and override status. Administrators can:

- inspect a skill and its files;
- upload a `SKILL.md` or a ZIP containing one skill;
- edit a built-in or writable skill;
- enable, disable, delete, or restore a skill;
- replace an existing writable override.

Uploads are extracted into a temporary directory and rejected for path traversal,
absolute paths, symbolic links, excessive file count or expanded size, invalid skill
names, missing `SKILL.md`, or multiple top-level skills. Installation uses an atomic
directory replacement so readers never observe a partial skill.

Catalog changes invalidate the server-side skill catalog immediately. New sessions
see changes immediately; an existing session refreshes the catalog before its next
user turn, without restarting the desktop application or backend.

## Agent installation

The model receives a constrained skill-install operation rather than arbitrary
filesystem access. It accepts a reviewed HTTPS source or an already uploaded
archive, resolves metadata, and requests approval showing the source, skill name,
version when available, and final managed destination.

Approval permits writes only through the skill manager into the writable `skill`
directory. It cannot write the application bundle, selected work directory, or an
arbitrary path. The existing one-time, session-wide, and reject approval decisions
apply. Successful installs invalidate the same catalog used by the Admin Panel.

## Upgrade and recovery

Application upgrades replace only the read-only built-in layer. Unmodified built-in
skills therefore update automatically. Writable overrides and tombstones persist.
“Restore built-in” removes both and immediately exposes the currently packaged
version.

The state file is written atomically and records schema version, disabled names, and
deleted built-in names. Corrupt state is quarantined and replaced with an empty
state while preserving skill files.

## Security and authorization

Skill management APIs require the existing admin authorization. Read endpoints never
return arbitrary filesystem paths; they expose logical skill identifiers and
relative file names. File reads and writes are resolved below the selected managed
root and checked after canonicalization.

Executable scripts are treated as skill content, not executed during upload or
validation. Their later execution continues through the existing tool and approval
boundaries.

