# Work Directories and Skill Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store new session data and output below the selected work directory and add a safe, immediately refreshed Admin Panel for built-in and user-managed skills.

**Architecture:** Keep packaged skills immutable and add a writable application-data overlay plus tombstone state. Resolve the two layers into one catalog, expose it through admin-only APIs, and route model-initiated installs through the same validated installer and approval boundary. Keep a global metadata index only for locating work directories while storing new session payloads under each work directory.

**Tech Stack:** Python 3.12+, FastAPI, Pydantic, React 19, TypeScript, Vite/Vitest, pytest, PyInstaller.

## Global Constraints

- User-selected work directories contain `session-data/` and `output/`, never `skill/` or `skills/`.
- The writable application-data directory is named exactly `skill`.
- Packaged `kimi_cli/skills` remains read-only and is never bulk-copied on startup.
- Skill mutation is copy-on-write, atomically installed, path-confined, and admin-authorized.
- macOS and Windows Settings remove all Paths controls.
- Existing sessions and existing environment files remain readable.
- The main checkout's unrelated `packaging/venvstacks.resolved.toml` modification must remain untouched.

---

### Task 1: Per-work-directory session paths

**Files:**
- Modify: `kimi-cli/src/kimi_cli/metadata.py`
- Modify: `kimi-cli/src/kimi_cli/session.py`
- Modify: `kimi-cli/src/kimi_cli/web/api/sessions.py`
- Modify: `kimi-cli/src/kimi_cli/web/runner/local.py`
- Test: `kimi-cli/tests/test_metadata.py`
- Test: `kimi-cli/tests/web/test_sessions.py`

**Interfaces:**
- Produces: `WorkDirMeta.sessions_dir` resolving new local sessions to `<work_dir>/session-data`.
- Produces: `ensure_work_dir_layout(path: Path) -> WorkDirLayout`.
- Produces: per-session worker environment containing `<work_dir>/output`.

- [ ] **Step 1: Write failing metadata and API tests**

Add tests asserting a local `WorkDirMeta(path=str(tmp_path))` returns
`tmp_path / "session-data"`, session creation creates both required directories,
and no skill directory is created.

- [ ] **Step 2: Run tests to verify RED**

Run: `cd kimi-cli && uv run pytest tests/test_metadata.py tests/web/test_sessions.py -q`

Expected: assertions fail because sessions still resolve below `KIMI_SHARE_DIR`.

- [ ] **Step 3: Implement the layout helper and compatibility lookup**

Introduce an immutable `WorkDirLayout` with `session_data_dir` and `output_dir`.
Use it for new local sessions. When loading by ID, retain the existing legacy
share-directory fallback after checking the new location.

- [ ] **Step 4: Pass the work-directory output path to local workers**

Build each worker environment from the session's canonical work directory and set
`KIMI_OUTPUT_DIR` to its `output` child without mutating the server process environment.

- [ ] **Step 5: Run targeted tests and commit**

Run: `cd kimi-cli && uv run pytest tests/test_metadata.py tests/web/test_sessions.py -q`

Expected: PASS.

Commit: `feat: store sessions under selected work directories`

### Task 2: Cross-platform default and Settings cleanup

**Files:**
- Modify: `packaging/app_main/paths.py`
- Modify: `packaging/app_main_win/paths.py`
- Modify: `packaging/app_main/settings_window.py`
- Modify: `packaging/app_main_win/settings.html`
- Modify: `packaging/app_main_win/settings_window.py`
- Modify: `packaging/app_main/dotenv_io.py`
- Modify: `kimi-cli/src/kimi_cli/web/api/sessions.py`
- Test: `packaging/tests/test_paths.py`
- Test: `packaging/tests/test_settings_paths_removed.py`

**Interfaces:**
- Produces: `default_documents_work_dir() -> Path`.
- Produces: startup directory endpoint returning the last valid work directory, then the platform Documents/OpenKimo default.

- [ ] **Step 1: Add failing cross-platform path and UI-absence tests**

Assert macOS defaults below the resolved Documents directory, Windows uses the known
folder resolver with a home/Documents fallback, and Settings source contains no
editable Paths fields.

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest packaging/tests/test_paths.py packaging/tests/test_settings_paths_removed.py -q`

Expected: FAIL because defaults point to application support and the controls exist.

- [ ] **Step 3: Implement defaults and remove controls**

Remove Paths sections and picker handlers from both Settings implementations. Remove
path keys from the editable allowlist while leaving `.env` parsing backward compatible.
Persist the last selected work directory through existing work-directory metadata.

- [ ] **Step 4: Run tests and commit**

Run: `python -m pytest packaging/tests/test_paths.py packaging/tests/test_settings_paths_removed.py -q`

Expected: PASS.

Commit: `feat: choose work directories when creating sessions`

### Task 3: Managed skill catalog and overlay state

**Files:**
- Create: `kimi-cli/src/kimi_cli/skill/manager.py`
- Create: `kimi-cli/tests/skill/test_manager.py`
- Modify: `kimi-cli/src/kimi_cli/skill/__init__.py`
- Modify: `packaging/app_main/paths.py`
- Modify: `packaging/app_main_win/paths.py`
- Modify: `packaging/app_main/server.py`
- Modify: `packaging/app_main_win/server.py`

**Interfaces:**
- Produces: `get_managed_skill_dir() -> Path`.
- Produces: `SkillManager.list_skills()`, `disable()`, `enable()`, `delete()`,
  `restore()`, `read_file()`, `write_file()`, and `install_archive()`.
- Produces: a monotonically increasing catalog revision.

- [ ] **Step 1: Write failing merge and state tests**

Cover built-in-only discovery, writable override precedence, disabled tombstones,
restore behavior, corrupt-state quarantine, copy-on-write edit, and atomic state writes.

- [ ] **Step 2: Run tests to verify RED**

Run: `cd kimi-cli && uv run pytest tests/skill/test_manager.py -q`

Expected: import failure because `skill.manager` does not exist.

- [ ] **Step 3: Implement path-confined manager**

Use normalized logical names, canonical child checks, an atomic JSON state file, and
temporary sibling directories followed by `os.replace`. Do not copy built-ins during
manager construction.

- [ ] **Step 4: Add the writable root to runtime discovery**

Resolve the writable layer before the built-in layer and filter built-ins using the
manager's disabled/deleted state. Feed its absolute path from desktop launchers through
`OPENKIMO_SKILL_DIR`.

- [ ] **Step 5: Run tests and commit**

Run: `cd kimi-cli && uv run pytest tests/skill/test_manager.py tests/skill -q`

Expected: PASS.

Commit: `feat: add managed skill overlay`

### Task 4: Secure archive validation and installation

**Files:**
- Create: `kimi-cli/src/kimi_cli/skill/archive.py`
- Create: `kimi-cli/tests/skill/test_archive.py`
- Modify: `kimi-cli/src/kimi_cli/skill/manager.py`

**Interfaces:**
- Produces: `inspect_skill_archive(data: BinaryIO, limits: ArchiveLimits) -> PreparedSkill`.
- Consumes: `SkillManager.install_prepared(prepared, replace: bool)`.

- [ ] **Step 1: Write failing hostile-archive tests**

Create in-memory ZIP cases for `../`, absolute paths, backslash traversal, symlinks,
duplicate normalized names, excessive entry count, excessive expanded bytes, missing
`SKILL.md`, and multiple skill roots. Include successful flat and directory archives.

- [ ] **Step 2: Run tests to verify RED**

Run: `cd kimi-cli && uv run pytest tests/skill/test_archive.py -q`

Expected: import failure because `skill.archive` does not exist.

- [ ] **Step 3: Implement bounded streaming extraction**

Inspect every `ZipInfo` before extraction, reject links and unsafe paths, enforce
limits while streaming, parse frontmatter, and return one normalized prepared skill.

- [ ] **Step 4: Run tests and commit**

Run: `cd kimi-cli && uv run pytest tests/skill/test_archive.py tests/skill/test_manager.py -q`

Expected: PASS.

Commit: `feat: validate and install skill archives`

### Task 5: Admin Skill API

**Files:**
- Create: `kimi-cli/src/kimi_cli/web/api/admin_skills.py`
- Create: `kimi-cli/tests/web/api/test_admin_skills.py`
- Modify: `kimi-cli/src/kimi_cli/web/api/__init__.py`
- Modify: `kimi-cli/src/kimi_cli/web/app.py`

**Interfaces:**
- Produces: `/api/admin/skills` list/upload routes.
- Produces: `/api/admin/skills/{name}` detail, edit, enable, disable, delete, and restore routes.

- [ ] **Step 1: Write failing authorization and behavior tests**

Assert anonymous and non-admin callers receive 401/403. Assert admins can list merged
origins, upload, edit with copy-on-write, disable, enable, delete, and restore. Assert
API responses expose logical identifiers and relative files but no absolute roots.

- [ ] **Step 2: Run tests to verify RED**

Run: `cd kimi-cli && uv run pytest tests/web/api/test_admin_skills.py -q`

Expected: 404 for every new route.

- [ ] **Step 3: Implement the router and schemas**

Use `require_admin`, bounded multipart reads, explicit conflict responses, and manager
methods only. Return the updated catalog revision after every mutation.

- [ ] **Step 4: Run tests and commit**

Run: `cd kimi-cli && uv run pytest tests/web/api/test_admin_skills.py -q`

Expected: PASS.

Commit: `feat: expose admin skill management api`

### Task 6: Admin Panel Skill tab

**Files:**
- Create: `kimi-cli/web/src/lib/api/apis/AdminSkillsApi.ts`
- Create: `kimi-cli/web/src/features/admin/admin-skills-panel.tsx`
- Create: `kimi-cli/web/src/features/admin/admin-skills-panel.test.tsx`
- Modify: `kimi-cli/web/src/features/admin/admin-page.tsx`

**Interfaces:**
- Consumes: Task 5 Admin Skill API.
- Produces: a `skills` Admin tab with upload, inspect, edit, enable/disable, delete, and restore actions.

- [ ] **Step 1: Write failing component tests**

Render built-in, modified, user-installed, and disabled rows. Exercise ZIP upload,
copy-on-write edit confirmation, disable, delete, and restore using real fetch responses.

- [ ] **Step 2: Run tests to verify RED**

Run: `cd kimi-cli/web && npm test -- admin-skills-panel.test.tsx`

Expected: module-not-found failure.

- [ ] **Step 3: Implement typed API client and panel**

Follow existing Admin Plugins panel patterns. Show origin/status badges, relative files,
safe text editing for `SKILL.md`, upload progress, conflict confirmation, and mutation
errors without exposing absolute server paths.

- [ ] **Step 4: Wire the tab and run tests**

Run: `cd kimi-cli/web && npm test -- admin-skills-panel.test.tsx`

Expected: PASS.

- [ ] **Step 5: Commit**

Commit: `feat: add skill management admin panel`

### Task 7: Model-requested skill installation

**Files:**
- Create: `kimi-cli/src/kimi_cli/tools/skill_install.py`
- Create: `kimi-cli/tests/tools/test_skill_install.py`
- Modify: `kimi-cli/src/kimi_cli/tools/__init__.py`
- Modify: `kimi-cli/src/kimi_cli/approval.py`

**Interfaces:**
- Produces: `InstallSkill` tool accepting an HTTPS URL and optional expected name.
- Consumes: the shared archive validator, manager, and approval runtime.

- [ ] **Step 1: Write failing approval and confinement tests**

Assert the tool rejects non-HTTPS and redirect-to-non-HTTPS sources, requests approval
before mutation, displays source/name/version/destination class, installs only after
approval, and leaves no files after rejection, timeout, or checksum/validation failure.

- [ ] **Step 2: Run tests to verify RED**

Run: `cd kimi-cli && uv run pytest tests/tools/test_skill_install.py -q`

Expected: import failure because the tool does not exist.

- [ ] **Step 3: Implement bounded download, preview, approval, and install**

Download into a temporary file with byte and timeout limits, validate before approval,
then call only `SkillManager.install_prepared`. Reuse existing approval decisions and
never expose a raw destination argument.

- [ ] **Step 4: Register the tool and run tests**

Run: `cd kimi-cli && uv run pytest tests/tools/test_skill_install.py tests/approval -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Commit: `feat: allow approved model skill installation`

### Task 8: Catalog refresh and packaging verification

**Files:**
- Modify: `kimi-cli/src/kimi_cli/soul/agent.py`
- Modify: `kimi-cli/src/kimi_cli/web/runner/local.py`
- Modify: `kimi-cli/src/kimi_cli/utils/pyinstaller.py`
- Modify: `packaging/build_windows.py`
- Modify: `packaging/build_macos.py`
- Test: `kimi-cli/tests/skill/test_refresh.py`
- Test: `packaging/tests/test_skill_packaging.py`

**Interfaces:**
- Consumes: catalog revision from Task 3.
- Produces: refresh-before-next-turn behavior and packaged built-in skill resources.

- [ ] **Step 1: Write failing refresh and packaging tests**

Assert a running session reloads discovery when revision changes, does not reload when
unchanged, and both packaging paths include `kimi_cli/skills` while writable data remains
outside the signed application.

- [ ] **Step 2: Run tests to verify RED**

Run: `cd kimi-cli && uv run pytest tests/skill/test_refresh.py -q`

Run: `python -m pytest packaging/tests/test_skill_packaging.py -q`

Expected: refresh assertions fail.

- [ ] **Step 3: Implement revision-aware refresh**

Before each new user turn, compare the active runtime revision with the manager revision
and rebuild only skill discovery/prompt/tool roots when changed.

- [ ] **Step 4: Verify packaging inputs**

Keep all shipped skills under `kimi-cli/src/kimi_cli/skills`; move the repository-root
`skills/grounding-dino-seg2` there and remove the now-empty root `skills/` source.

- [ ] **Step 5: Run focused and full verification**

Run:

```bash
cd kimi-cli
uv run pytest tests/skill tests/tools/test_skill_install.py tests/web/api/test_admin_skills.py -q
cd web
npm test
npm run build
```

Then run:

```bash
python -m pytest packaging/tests -q
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

Commit: `feat: refresh and package managed skills`

### Task 9: Final regression and review

**Files:**
- Modify only files required by failures found during verification.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: release-ready branch with evidence.

- [ ] **Step 1: Run backend regression suites**

Run: `cd kimi-cli && uv run pytest tests/skill tests/web tests/tools -q`

Expected: PASS.

- [ ] **Step 2: Run frontend validation**

Run: `cd kimi-cli/web && npm test && npm run lint && npm run build`

Expected: PASS.

- [ ] **Step 3: Run desktop-focused validation**

Run: `python -m pytest packaging/tests -q`

Expected: PASS.

- [ ] **Step 4: Inspect the final diff**

Run: `git status --short && git diff --check && git diff --stat main...HEAD`

Expected: no unstaged files, no whitespace errors, and only scoped changes.

- [ ] **Step 5: Request code review and address confirmed findings**

Review path confinement, ZIP validation, authorization, migration fallback, and
cross-platform path selection. Add a failing regression test before every fix.

