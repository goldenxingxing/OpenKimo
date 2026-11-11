# OpenKimo macOS App Packaging

Builds a self-contained `.app` and `.dmg` that runs OpenKimo's local mode
without requiring the user to install Python, Node, or any other runtime.

## Prerequisites

- macOS 14.0+ (Sonoma or later)
- macOS host matching target architecture: Apple Silicon (arm64) for arm64 DMG, Intel (x86_64) for x86_64 DMG. Cross-compiling from arm64 host to x86_64 wheels is not supported.
- Xcode Command Line Tools (`xcode-select --install`) — for `cc`, `codesign`, `hdiutil`, `iconutil`, `sips`
- [`uv`](https://github.com/astral-sh/uv) (`brew install uv`) — workspace-aware Python build; `uvx` runs `venvstacks` in isolation

## Building

```bash
cd packaging

# Default OpenKimo build
python3 build.py

# Iterating on packaging code only (skip slow venv rebuild)
python3 build.py --skip-venv

# Just rebuild the DMG from an existing .app
python3 build.py --dmg-only

# Build for Intel x86_64 (must run on an Intel Mac)
python3 build.py --arch x86_64
```

Output:

```
dist/
├── OpenKimo.app
└── OpenKimo-<version>-<arch>.dmg    # arch = arm64 or x86_64
```

## Dependency locks (not reproducible yet)

`venvstacks lock` writes a fully pinned, hashed requirement set per layer and
per platform into `packaging/requirements/`. Those files are **not committed**
(`packaging/.gitignore`), and `build.py` runs `venvstacks lock` unconditionally,
so every build re-resolves from PyPI. Two consequences:

* **A rebuild of the same tag can ship different dependencies.** kimo-cli's
  `uv.lock` pins `anthropic 0.78.0`, which is what CI tests run against;
  v0.1.41 shipped `anthropic 1.3.0`, because `kosong` asks for
  `anthropic>=0.78.0` with no upper bound. That major bump also pulled the HTTP
  stack from `httpx` to `httpx2`. Nothing tested that combination — its 35
  symbols still import, which was checked by hand, but nothing beyond that.
* **The shipped set is only knowable after the fact.** Each release job now
  uploads its `packaging/requirements/` as a `venvstacks-locks-<ARCH>`
  artifact, so it can be retrieved and diffed against the previous release.

To close this, all three steps are needed together:

1. Commit the locks for **both** platforms. They are platform-specific
   (`...-macosx_arm64.txt`, `...-macosx_x86_64.txt`) and each must be generated
   on its own architecture — running the locker under Rosetta still produces
   arm64 files, so the x86_64 one has to come off an Intel machine or the
   `build-macos-intel` job's artifact.
2. Switch `build.py`'s lock step to `venvstacks lock --if-needed`, which skips
   layers whose lock is still valid. Verified locally: with inputs unchanged it
   leaves the files untouched, and adding a requirement re-locks that layer and
   only that layer.
3. Stop caching the locks. `release.yml` caches `packaging/.build`, and with
   `--if-needed` a cache hit would freeze dependencies at whatever the cache
   holds — trading "resolved fresh every time" for "resolved whenever the cache
   last missed", which is worse. The committed files must be the only source.

Doing 2 without 1 is worse than doing nothing: the arm64 build would honour a
committed lock while the Intel build kept floating, so the two installers for
one tag would genuinely diverge — which today they do not (verified by
comparing the published v0.1.41 arm64 and x86_64 DMGs: identical package sets).

## White-Label / OEM builds

All branding is parameterized at build time. Override any field from
`brand.toml` via CLI flags:

```bash
python3 build.py \
  --app-name="ACME Agent" \
  --bundle-id=com.acme.agent \
  --icon=./assets/acme-icon.png \
  --logo=./assets/acme-logo.png \
  --favicon=./assets/acme-fav.png \
  --brand-name="ACME Agent" \
  --page-title="ACME Agent" \
  --copyright="© 2026 ACME Inc."
```

When you change `--app-name`, **every** derived path follows:

| | Default | `--app-name="ACME Agent"` |
|--|--|--|
| Bundle | `OpenKimo.app` | `ACME Agent.app` |
| DMG | `OpenKimo-<v>.dmg` | `ACME Agent-<v>.dmg` |
| Application Support | `~/Library/Application Support/OpenKimo/` | `~/Library/Application Support/ACME Agent/` |
| Logs | `~/Library/Logs/OpenKimo/` | `~/Library/Logs/ACME Agent/` |
| Console command | `openkimo` | `acme-agent` |

This means two differently-branded builds can coexist on one Mac without
sharing data.

## Bundle structure

```
OpenKimo.app/
├── Contents/
│   ├── Info.plist
│   ├── MacOS/
│   │   └── OpenKimo                    # C launcher (dlopen libpython + Py_BytesMain)
│   └── Resources/
│       ├── AppIcon.icns
│       ├── Credits.rtf                 # About panel
│       ├── brand.json                  # runtime brand metadata
│       ├── openkimo-cli                # shell wrapper for `openkimo pip` / `openkimo python`
│       ├── kimo_cli/                   # kimo_cli source (incl. web/static)
│       ├── app_main/                   # menu bar app + supervisor
│       ├── LICENSE.txt
│       └── runtimes/
│           ├── cpython-3.12/           # Layer 1: Python runtime
│           ├── framework-kimo/         # Layer 2: business deps
│           └── app-openkimo/           # Layer 3: rumps + PyObjC
```

## Runtime layout

The `.app` is read-only and signed. User-mutable state lives under
`~/Library/Application Support/<AppName>/`:

```
~/Library/Application Support/OpenKimo/
├── .env                                # LLM keys + model + paths (Settings window writes here)
├── pip.conf                            # forces `--user` installs
├── python-userbase/                    # user-installed pip packages overlay
│   └── lib/python3.12/site-packages/
├── work/                               # default work_dir for new sessions
├── sessions/                           # session history + users.db
└── output/

~/Library/Logs/OpenKimo/
├── server.log                          # uvicorn stdout/stderr
└── pip.log                             # Install Package… output
```

## Code signing

The default build uses **ad-hoc** signing (`codesign --sign -`). Users will
see a Gatekeeper warning on first launch and need to right-click → Open.

For release distribution, signing + notarization is handled separately by
`build_release.py` (TBD); it expects:

```bash
export DEVELOPER_ID="Developer ID Application: Your Name (TEAMID)"
export NOTARY_PROFILE="AC_NOTARY"   # set up via `xcrun notarytool store-credentials`
python3 build_release.py
```

## Windows builds

Windows packaging is driven by `build_windows.py` (parallel to `build.py`)
and produces a single-file installer (`OpenKimoSetup-<version>.exe`).

```bash
# Default build (must run on Windows for the .exe + installer steps;
# on macOS the script stages the layout and skips PyInstaller/Inno Setup).
python3 build_windows.py

# Layout-only dry run on macOS (useful when iterating on packaging code):
python3 build_windows.py --skip-installer --skip-frontend
```

What the pipeline produces:

```
build/runtime-staging/
├── OpenKimo.exe                 # PyInstaller launcher (Windows-built)
├── brand.json                   # runtime brand metadata
├── TrayIcon.ico
└── runtime/
    ├── python/                  # python-build-standalone (CPython)
    ├── site-packages/           # kimo-cli wheel + pystray/Pillow/pywebview/playwright
    ├── kimo_cli/kimo_cli/...    # kimo_cli source incl. web/static
    ├── packaging/{__init__,app_main,app_main_win}/
    ├── playwright-browsers/     # bundled chromium
    └── OpenKimo.ico

dist-win/
└── OpenKimoSetup-<version>.exe  # Inno Setup output
```

Prerequisites on Windows:

- Python 3.12+ on PATH (for running `build_windows.py` itself).
- `uv` (`pipx install uv` or the Astral installer) — used to build the
  kimo-cli wheel.
- `pyinstaller` (`pip install pyinstaller`) — bundles the thin launcher.
- [Inno Setup 6](https://jrsoftware.org/isdl.php) — compiles the installer.
- Node.js LTS (for the frontend build; omit with `--skip-frontend`).

Runtime deps installed into `runtime/site-packages/` are listed in
`packaging/requirements-windows.txt`.

Releases are built in the public `goldenxingxing/OpenKimo-releases` repo, where standard Actions runners are free. After pushing a `v*` tag here, run:

```bash
gh workflow run release.yml -R goldenxingxing/OpenKimo-releases -f tag=v0.2.2
```

It builds all three artifacts in parallel — macOS arm64 DMG, macOS x86_64 DMG, and Windows x64 installer .exe — plus the Docker images, and uploads them to the GitHub Release there. This repo's `.github/workflows/release.yml` is kept as a manual fallback only.

## Troubleshooting

- **`venvstacks lock` fails with "no usable wheels"** — usually means a dep
  is sdist-only on the current platform; `build.py:_lock_with_sdist_retry`
  handles this by pre-building wheels via `uv build`.
- **Menu bar icon missing on macOS Sonoma+** — must NOT happen because the
  C launcher dlopens libpython rather than spawning python. If it does,
  check that `Contents/MacOS/<AppName>` is the launcher binary and not a
  shell script.
- **`pip install` from chat agent doesn't appear in server** — check that
  `PYTHONUSERBASE` and `PIP_CONFIG_FILE` are set in the supervisor's
  environment and inherited by uvicorn.
