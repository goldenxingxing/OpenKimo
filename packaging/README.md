# OpenKimo macOS App Packaging

Builds a self-contained `.app` and `.dmg` that runs OpenKimo's local mode
without requiring the user to install Python, Node, or any other runtime.

## Prerequisites

- macOS 14.0+ (Sonoma or later)
- Apple Silicon (arm64) — Universal builds are out of scope for now
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
```

Output:

```
dist/
├── OpenKimo.app
└── OpenKimo-<version>.dmg
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
