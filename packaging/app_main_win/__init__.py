"""OpenKimo Windows entry package.

Contains the system-tray supervisor that launches uvicorn, plus the
pywebview-based Settings window. Mirrors the macOS ``app_main`` package;
shared modules (server, dotenv_io, configtoml, seed_branding, userenv)
are re-exported from there via thin shims to keep the macOS code path
bit-identical.
"""
