#!/usr/bin/env bash
set -euo pipefail

install_uv() {
  if command -v curl >/dev/null 2>&1; then
    if curl -fsSL https://astral.sh/uv/install.sh | sh; then
      return
    fi
  fi

  if command -v wget >/dev/null 2>&1; then
    if wget -qO- https://astral.sh/uv/install.sh | sh; then
      return
    fi
  fi

  echo "Error: failed to install uv via curl or wget." >&2
  exit 1
}

if command -v uv >/dev/null 2>&1; then
  UV_BIN="uv"
else
  install_uv
  UV_BIN="uv"
fi

# The installer puts uv in ~/.local/bin and does not modify this process's
# PATH, so a fresh install looked like a failed one on the very next check.
export PATH="$HOME/.local/bin:$PATH"
if ! command -v "$UV_BIN" >/dev/null 2>&1; then
  echo "Error: uv not found after installation." >&2
  exit 1
fi

"$UV_BIN" tool install --python 3.13 kimo-cli
