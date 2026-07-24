# Windows Branding and Hidden Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the canonical blue-ring favicon in Windows packages, migrate only the legacy built-in black/white icon, and run the Windows uvicorn process without a visible console.

**Architecture:** Extend the Windows branding seed to carry the canonical Logo/Favicon and a legacy asset hash. Keep migration decisions in the shared pure-Python branding seeder, and isolate Windows process flags behind a small platform helper that can be tested on macOS.

**Tech Stack:** Python 3.12, stdlib `base64`/`hashlib`/`sqlite3`, Windows `subprocess` creation flags, `pytest`.

## Global Constraints

- `kimi-cli/web/public/logo.png` remains the only raster source for app icon, Web logo, and favicon.
- Existing user-uploaded Logo/Favicon values must survive upgrades.
- Only the legacy built-in asset with SHA-256 `dbd00e2ad61ea8832ef0b024662a4a8a5d1b66f0599d5d42e1c9688b9d4cfdf6` is migrated.
- Windows server stdout and stderr must continue writing to `server.log`.
- POSIX subprocess behavior must remain unchanged.
- `packaging/venvstacks.resolved.toml` is unrelated and must remain unstaged.

---

### Task 1: Add canonical assets to the Windows branding seed

**Files:**
- Modify: `packaging/build_windows.py`
- Create: `tests/test_windows_packaging.py`

**Interfaces:**
- `BuildConfig.logo` and `BuildConfig.favicon` are absolute `Path` values read from `[app]` in `packaging/brand.toml`.
- `write_brand_json()` writes `branding_seed.logo`, `branding_seed.favicon`, and `branding_legacy_asset_sha256`.

- [ ] **Step 1: Write failing Windows brand JSON tests**

Create a temporary `BuildConfig`, call `write_brand_json`, and assert:

```python
seed = json.loads(output.read_text())["branding_seed"]
assert seed["logo"].startswith("data:image/png;base64,")
assert seed["favicon"] == seed["logo"]
assert json.loads(output.read_text())["branding_legacy_asset_sha256"] == [
    "dbd00e2ad61ea8832ef0b024662a4a8a5d1b66f0599d5d42e1c9688b9d4cfdf6"
]
```

Also assert `parse_args([]).logo == parse_args([]).favicon == CANONICAL_ICON`.

- [ ] **Step 2: Run RED**

Run:

```bash
pytest tests/test_windows_packaging.py -q
```

Expected: FAIL because `BuildConfig` has no `logo` or `favicon` and Windows `brand.json` omits both.

- [ ] **Step 3: Implement the Windows seed**

Add `logo` and `favicon` to `BuildConfig`, CLI options matching the macOS builder, a size-limited PNG Data URL helper, and:

```python
LEGACY_BUILTIN_ASSET_SHA256 = [
    "dbd00e2ad61ea8832ef0b024662a4a8a5d1b66f0599d5d42e1c9688b9d4cfdf6"
]
```

Write both Data URLs and the legacy hash list into `brand.json`. Resolve all three asset paths relative to `packaging/brand.toml` through the existing `ROOT / value` convention.

- [ ] **Step 4: Run GREEN**

Run:

```bash
pytest tests/test_windows_packaging.py tests/test_brand_icon.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add packaging/build_windows.py tests/test_windows_packaging.py
git commit -m "fix: seed canonical Windows branding assets"
```

### Task 2: Migrate only the legacy built-in favicon

**Files:**
- Modify: `packaging/app_main/seed_branding.py`
- Modify: `tests/test_windows_packaging.py`

**Interfaces:**
- `_data_url_sha256(value: str) -> str | None` returns the decoded payload hash or `None`.
- `_load_legacy_asset_hashes(paths: AppPaths) -> frozenset[str]` reads and validates the packaged hash list.
- `seed_if_needed()` replaces non-empty `logo`/`favicon` only when their decoded hash is in that set.

- [ ] **Step 1: Write failing migration tests**

Use a temporary `brand.json`, `.env`, sessions directory, and `SimpleNamespace` paths object. Seed SQLite with:

```python
legacy = "data:image/png;base64," + base64.b64encode(legacy_bytes).decode()
custom = "data:image/png;base64," + base64.b64encode(b"custom").decode()
```

Assert an empty field receives the packaged value, a legacy-hash field is replaced, and `custom` remains unchanged.

- [ ] **Step 2: Run RED**

Run:

```bash
pytest tests/test_windows_packaging.py -q
```

Expected: the legacy value remains unchanged.

- [ ] **Step 3: Implement hash-based migration**

Decode only valid `data:*;base64,` values with `base64.b64decode(..., validate=True)`, hash with SHA-256, and compare against the packaged allowlist. Invalid or non-Data-URL values must be preserved. Keep `version` as the existing always-synchronized build-derived field.

- [ ] **Step 4: Run GREEN**

Run:

```bash
pytest tests/test_windows_packaging.py tests/test_brand_icon.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add packaging/app_main/seed_branding.py tests/test_windows_packaging.py
git commit -m "fix: migrate legacy built-in favicon"
```

### Task 3: Hide the Windows uvicorn console

**Files:**
- Modify: `packaging/app_main/server.py`
- Modify: `tests/test_windows_packaging.py`

**Interfaces:**
- `_popen_new_group_kwargs(platform: str) -> dict` returns Windows creation flags or POSIX session arguments.
- Windows flags equal `0x00000200 | 0x08000000`.

- [ ] **Step 1: Write failing process flag tests**

Assert:

```python
windows = server._popen_new_group_kwargs("win32")
assert windows["creationflags"] & 0x00000200
assert windows["creationflags"] & 0x08000000
assert server._popen_new_group_kwargs("darwin") == {"start_new_session": True}
```

- [ ] **Step 2: Run RED**

Run:

```bash
pytest tests/test_windows_packaging.py -q
```

Expected: FAIL because the helper and `CREATE_NO_WINDOW` flag do not exist.

- [ ] **Step 3: Implement the platform helper**

Use numeric Win32 fallbacks so the helper remains importable and testable on macOS:

```python
def _popen_new_group_kwargs(platform: str) -> dict:
    if platform == "win32":
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        return {"creationflags": new_group | no_window}
    return {"start_new_session": True}
```

Initialize `_POPEN_NEW_GROUP_KWARGS` from `sys.platform`; leave logging redirection and shutdown fallback unchanged.

- [ ] **Step 4: Run full focused verification**

Run:

```bash
pytest tests/test_windows_packaging.py tests/test_brand_icon.py -q
python3 -m py_compile packaging/build_windows.py packaging/app_main/seed_branding.py packaging/app_main/server.py
git diff --check
```

Expected: all tests pass and all static checks exit 0.

- [ ] **Step 5: Commit**

```bash
git add packaging/app_main/server.py tests/test_windows_packaging.py
git commit -m "fix: hide Windows runtime console"
```

### Task 4: Integrate and push

**Files:**
- No additional source files.

- [ ] **Step 1: Verify unrelated changes remain untouched**

Run `git status --short` and confirm `packaging/venvstacks.resolved.toml` is not staged.

- [ ] **Step 2: Merge the isolated branch into `main`**

Use a fast-forward merge, rerun Task 3 verification on `main`, then remove the owned worktree and feature branch.

- [ ] **Step 3: Push**

Run:

```bash
git push release main
```

Expected: non-force push succeeds.

- [ ] **Step 4: Report Windows acceptance boundary**

State that source-level and static verification passed. Do not claim the visible Windows behavior is verified until a new Windows installer is built and tested on Windows.
