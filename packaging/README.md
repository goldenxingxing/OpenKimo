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
│       ├── kimi_cli/                   # kimi_cli source (incl. web/static)
│       ├── app_main/                   # menu bar app + supervisor
│       ├── LICENSE.txt
│       └── runtimes/
│           ├── cpython-3.12/           # Layer 1: Python runtime
│           ├── framework-kimi/         # Layer 2: business deps
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
    ├── site-packages/           # kimi-cli wheel + pystray/Pillow/pywebview/playwright
    ├── kimi_cli/kimi_cli/...    # kimi_cli source incl. web/static
    ├── packaging/{__init__,app_main,app_main_win}/
    ├── playwright-browsers/     # bundled chromium
    └── OpenKimo.ico

dist-win/
└── OpenKimoSetup-<version>.exe  # Inno Setup output
```

Prerequisites on Windows:

- Python 3.12+ on PATH (for running `build_windows.py` itself).
- `uv` (`pipx install uv` or the Astral installer) — used to build the
  kimi-cli wheel.
- `pyinstaller` (`pip install pyinstaller`) — bundles the thin launcher.
- [Inno Setup 6](https://jrsoftware.org/isdl.php) — compiles the installer.
- Node.js LTS (for the frontend build; omit with `--skip-frontend`).

Runtime deps installed into `runtime/site-packages/` are listed in
`packaging/requirements-windows.txt`.

On tag push, CI builds all three artifacts in parallel — macOS arm64 DMG, macOS x86_64 DMG, and Windows x64 installer .exe — and uploads them to the GitHub Release. See `.github/workflows/release.yml`.

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
