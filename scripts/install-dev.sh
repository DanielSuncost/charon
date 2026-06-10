#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
LINK_PATH="${BIN_DIR}/charon"
RUST_MANIFEST="${ROOT}/crates/charon-tui/Cargo.toml"
RUST_BIN="${ROOT}/crates/charon-tui/target/release/charon"
INSTALL_PLAYWRIGHT=1
FORCE=0

usage() {
  cat <<EOF
Charon dev installer

Usage:
  ./scripts/install-dev.sh [options]

Options:
  --no-playwright Skip Playwright Python package + Chromium browser install
  --playwright    Force Playwright Python package + Chromium browser install
  --force         Reinstall Python deps and rebuild Rust TUI
  -h, --help      Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --playwright)
      INSTALL_PLAYWRIGHT=1
      shift
      ;;
    --no-playwright)
      INSTALL_PLAYWRIGHT=0
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required command not found: $1" >&2
    exit 1
  fi
}

need_cmd cargo

mkdir -p "$BIN_DIR"

VENV_DIR="${ROOT}/.venv"

# Pick a working Python: prefer 3.13, fall back to 3.12, then generic python3
pick_python() {
  for candidate in python3.13 python3.12 python3; do
    local p
    p="$(command -v "$candidate" 2>/dev/null)" || continue
    if "$p" -c "import platform; assert platform.mac_ver()[0]" 2>/dev/null || \
       "$p" -c "import sys; sys.platform != 'darwin'" 2>/dev/null; then
      echo "$p"
      return
    fi
  done
  # Last resort
  command -v python3
}

PYTHON="$(pick_python)"
echo "Using Python: $PYTHON ($("$PYTHON" --version 2>&1))"

# Create venv if missing or forced
if [[ ! -d "$VENV_DIR" || "$FORCE" -eq 1 ]]; then
  echo "Creating virtual environment..."
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV_DIR" --python "$PYTHON"
  else
    "$PYTHON" -m venv "$VENV_DIR"
  fi
fi

# Activate venv for the rest of this script
export VIRTUAL_ENV="$VENV_DIR"
export PATH="${VENV_DIR}/bin:${PATH}"

PYTHON_OK=0
if "${VENV_DIR}/bin/python3" -c "import httpx" >/dev/null 2>&1 && [[ "$FORCE" -eq 0 ]]; then
  PYTHON_OK=1
fi

if [[ "$PYTHON_OK" -eq 0 ]]; then
  echo "Installing Python dependencies..."
  if command -v uv >/dev/null 2>&1; then
    uv pip install -r "$ROOT/requirements.txt"
  else
    "${VENV_DIR}/bin/python3" -m pip install -r "$ROOT/requirements.txt"
  fi
else
  echo "Python dependencies already available."
fi

if [[ "$INSTALL_PLAYWRIGHT" -eq 1 ]]; then
  echo "Ensuring Playwright + Chromium are installed..."
  if command -v uv >/dev/null 2>&1; then
    uv pip install playwright
  else
    "${VENV_DIR}/bin/python3" -m pip install playwright
  fi
  "${VENV_DIR}/bin/python3" -m playwright install chromium
fi

if [[ ! -x "$RUST_BIN" || "$FORCE" -eq 1 ]]; then
  echo "Building Rust TUI..."
  cargo build --release --manifest-path "$RUST_MANIFEST"
else
  echo "Rust TUI already built."
fi

if [[ ! -x "$RUST_BIN" ]]; then
  echo "Error: expected Rust binary not found at $RUST_BIN" >&2
  exit 1
fi

ln -sf "$ROOT/charon" "$LINK_PATH"
chmod +x "$ROOT/charon"

echo
echo "Installed Charon dev launcher: $LINK_PATH"
if [[ ":${PATH}:" != *":${BIN_DIR}:"* ]]; then
  echo "Note: ${BIN_DIR} is not on your PATH. Add this to your shell rc:"
  echo "  export PATH=\"${BIN_DIR}:\$PATH\""
fi

echo
echo "Run:"
echo "  charon"
echo
if [[ "$INSTALL_PLAYWRIGHT" -eq 0 ]]; then
  echo "Browser support was skipped. For x.com/browser features, run:"
  echo "  ./scripts/install-dev.sh --playwright"
fi
