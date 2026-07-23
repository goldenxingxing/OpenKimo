# Unified Brand Icon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the black-background, blue-circle, white-letter artwork the single checked-in default icon for the Web UI and macOS/Windows packages while preserving administrator branding overrides.

**Architecture:** The canonical PNG moves to `kimi-cli/web/public/logo.png`, where Vite can serve and copy it without custom asset plumbing. Both desktop packaging scripts consume that same path through `packaging/brand.toml`; platform-specific `.icns`, menu-bar PNG, `.ico`, and Web `dist/logo.png` files remain generated derivatives.

**Tech Stack:** Python 3.12, pytest, TOML, Vite/TypeScript, macOS `sips`, `iconutil`, `codesign`, and `hdiutil`.

## Global Constraints

- `kimi-cli/web/public/logo.png` is the only source image checked into the code for the built-in product icon.
- The canonical image contains the current 1024×1024 black-background, blue-circle, white-letter artwork.
- Delete `packaging/icon.png`.
- Administrator-provided custom logo and favicon values continue to override the built-in defaults.
- Generated Web and platform icon files are derivatives, not independent source artwork.

---

## File Structure

- Create `tests/test_brand_icon.py`: regression tests for the canonical source, packaging configuration, and removal of the duplicate source.
- Modify `kimi-cli/web/public/logo.png`: replace the old black-and-white artwork with the canonical blue-circle artwork.
- Modify `packaging/brand.toml`: point desktop icon, branding logo, and favicon defaults at the canonical Web asset.
- Delete `packaging/icon.png`: remove the duplicate source image.

### Task 1: Enforce a single canonical built-in icon

**Files:**
- Create: `tests/test_brand_icon.py`
- Modify: `kimi-cli/web/public/logo.png`
- Modify: `packaging/brand.toml`
- Delete: `packaging/icon.png`

**Interfaces:**
- Consumes: `[app].icon`, `[app].logo`, and `[app].favicon` from `packaging/brand.toml`, resolved relative to `packaging/` by both desktop build scripts.
- Produces: one 1024×1024 PNG at `kimi-cli/web/public/logo.png` used by Vite and both desktop packaging systems.

- [ ] **Step 1: Write the failing regression test**

```python
from __future__ import annotations

import struct
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_ICON = ROOT / "kimi-cli" / "web" / "public" / "logo.png"
OLD_PACKAGING_ICON = ROOT / "packaging" / "icon.png"
EXPECTED_REFERENCE = ROOT / "packaging" / "brand.toml"


def _png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_builtin_brand_icon_has_one_canonical_source() -> None:
    brand = tomllib.loads(EXPECTED_REFERENCE.read_text())
    expected_path = "../kimi-cli/web/public/logo.png"

    assert brand["app"]["icon"] == expected_path
    assert brand["app"]["logo"] == expected_path
    assert brand["app"]["favicon"] == expected_path
    assert CANONICAL_ICON.is_file()
    assert _png_dimensions(CANONICAL_ICON) == (1024, 1024)
    assert not OLD_PACKAGING_ICON.exists()
```

- [ ] **Step 2: Run the test and confirm the old layout fails**

Run:

```bash
uv run --python 3.12 pytest tests/test_brand_icon.py -v
```

Expected: FAIL because `brand.toml` still points to `icon.png`, the Web image is 128×128, and `packaging/icon.png` still exists.

- [ ] **Step 3: Move the blue-circle artwork over the old Web artwork**

Run from the repository root:

```bash
mv -f packaging/icon.png kimi-cli/web/public/logo.png
```

This preserves the exact approved blue-circle PNG bytes, deletes the duplicate
source, and replaces the old black-and-white image.

- [ ] **Step 4: Configure every built-in desktop branding consumer**

Change the `[app]` section of `packaging/brand.toml` to:

```toml
[app]
name = "OpenKimo"
bundle_id = "ai.openkimo.app"
copyright = "© 2026 OpenKimo Contributors. Apache License 2.0."
min_macos_version = "14.0"
icon = "../kimi-cli/web/public/logo.png"
logo = "../kimi-cli/web/public/logo.png"
favicon = "../kimi-cli/web/public/logo.png"
```

The existing branding API and Web precedence rules are unchanged, so custom
administrator values still override these defaults.

- [ ] **Step 5: Run the regression test**

Run:

```bash
uv run --python 3.12 pytest tests/test_brand_icon.py -v
```

Expected: `1 passed`.

- [ ] **Step 6: Commit the submodule asset first**

Run:

```bash
git -C kimi-cli add web/public/logo.png
git -C kimi-cli commit -m "fix(web): unify the default product icon"
```

Expected: one modified binary asset committed in `kimi-cli`.

- [ ] **Step 7: Commit the parent configuration, test, deletion, and submodule pointer**

Run:

```bash
git add tests/test_brand_icon.py packaging/brand.toml packaging/icon.png kimi-cli
git commit -m "fix: use one canonical brand icon"
```

Expected: the test and configuration are added, `packaging/icon.png` is deleted,
and the parent records the new `kimi-cli` commit.

### Task 2: Verify Web output and custom-branding compatibility

**Files:**
- Verify: `kimi-cli/web/public/logo.png`
- Generated: `kimi-cli/web/dist/logo.png`
- Test: `tests/test_branding_api.py`

**Interfaces:**
- Consumes: Vite's existing `public/` copying behavior and the branding API's `logo`/`favicon` nullable override fields.
- Produces: a Web distribution whose `/logo.png` exactly matches the canonical source without changing administrator override behavior.

- [ ] **Step 1: Run the branding regression suites**

Run:

```bash
uv run --python 3.12 pytest tests/test_brand_icon.py tests/test_branding_api.py -v
```

Expected: all tests pass, including tests that accept, persist, return, and
reset custom logo and favicon values.

- [ ] **Step 2: Build the Web frontend**

Run:

```bash
npm run build
```

from `kimi-cli/web`.

Expected: TypeScript and Vite complete successfully and emit
`kimi-cli/web/dist/logo.png`.

- [ ] **Step 3: Prove the emitted Web icon is derived from the canonical source**

Run:

```bash
cmp kimi-cli/web/public/logo.png kimi-cli/web/dist/logo.png
```

Expected: exit code `0` and no output.

- [ ] **Step 4: Run Web lint and unit tests**

Run:

```bash
npm run lint
npm run test:unit
```

from `kimi-cli/web`.

Expected: both commands pass.

### Task 3: Rebuild and inspect the macOS ARM64 package

**Files:**
- Generated: `dist/OpenKimo.app`
- Generated: `dist/OpenKimo-0.1.18-arm64.dmg`

**Interfaces:**
- Consumes: canonical `kimi-cli/web/public/logo.png` through the three paths in `packaging/brand.toml`.
- Produces: a signed ARM64 app and valid DMG whose Dock icon, menu-bar icon, Web logo, favicon, and branding seed derive from the canonical source.

- [ ] **Step 1: Build the current ARM64 release**

Run:

```bash
uv run --python 3.12 python build.py --arch arm64 --version 0.1.18
```

from `packaging/`.

Expected: successful `.app` and `OpenKimo-0.1.18-arm64.dmg` generation.

- [ ] **Step 2: Verify version, architecture, signature, and disk image**

Run:

```bash
plutil -extract CFBundleShortVersionString raw dist/OpenKimo.app/Contents/Info.plist
lipo -archs dist/OpenKimo.app/Contents/MacOS/OpenKimo
codesign --verify --deep --strict --verbose=2 dist/OpenKimo.app
hdiutil verify dist/OpenKimo-0.1.18-arm64.dmg
```

Expected: version `0.1.18`, architecture `arm64`, valid code signature, and a
valid DMG checksum.

- [ ] **Step 3: Verify the packaged branding seed uses the canonical artwork**

Run a Python 3.12 check that decodes both Data URLs and compares them to the
source PNG:

```bash
uv run --python 3.12 python - <<'PY'
import base64
import json
from pathlib import Path

root = Path.cwd()
source = (root / "kimi-cli/web/public/logo.png").read_bytes()
brand = json.loads(
    (root / "dist/OpenKimo.app/Contents/Resources/brand.json").read_text()
)
seed = brand["branding_seed"]
for key in ("logo", "favicon"):
    encoded = seed[key].split(",", 1)[1]
    assert base64.b64decode(encoded) == source, key
print("packaged logo and favicon match canonical source")
PY
```

Expected: `packaged logo and favicon match canonical source`.

- [ ] **Step 4: Visually inspect source and generated icons**

Open these files with the image viewer:

- `kimi-cli/web/public/logo.png`
- `dist/OpenKimo.app/Contents/Resources/MenuBarIcon.png`
- one rendered 512×512 representation of
  `dist/OpenKimo.app/Contents/Resources/AppIcon.icns`

Expected: all show the black background, blue circle, and white letter without
using the deleted black-and-white artwork.

- [ ] **Step 5: Confirm the worktree contains no unintended build changes**

Run:

```bash
git status --short
git -C kimi-cli status --short
```

Expected: only known local build metadata such as
`packaging/venvstacks.resolved.toml` may remain modified; generated `dist/`
artifacts are untracked or ignored.

