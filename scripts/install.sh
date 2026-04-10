#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DEV="$ROOT/scripts/install-dev.sh"
AUTO_YES=0
INSTALL_PLAYWRIGHT=1
FORCE=0
INSTALL_TMUX=1

usage() {
  cat <<EOF
Charon installer (macOS + Ubuntu)

Usage:
  ./scripts/install.sh [options]

Options:
  --yes, -y        Non-interactive where possible
  --no-playwright  Skip Playwright Python package + Chromium browser install
  --playwright     Force Playwright Python package + Chromium browser install
  --force          Reinstall deps + rebuild via install-dev.sh
  --no-tmux        Skip installing tmux as an optional helper dependency
  -h, --help       Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)
      AUTO_YES=1
      shift
      ;;
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
    --no-tmux)
      INSTALL_TMUX=0
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

log() { echo "[charon-install] $*"; }
need_sudo() {
  if [[ "$(id -u)" -ne 0 ]]; then
    sudo "$@"
  else
    "$@"
  fi
}
cmd_exists() { command -v "$1" >/dev/null 2>&1; }

ensure_path() {
  local line='export PATH="$HOME/.local/bin:$PATH"'
  local shell_rc=""
  if [[ -n "${ZSH_VERSION:-}" || "${SHELL:-}" == *"zsh" ]]; then
    shell_rc="$HOME/.zshrc"
  elif [[ -n "${BASH_VERSION:-}" || "${SHELL:-}" == *"bash" ]]; then
    shell_rc="$HOME/.bashrc"
  fi

  # Already on PATH?
  if echo "$PATH" | tr ':' '\n' | grep -qx "$HOME/.local/bin"; then
    return
  fi

  if [[ -n "$shell_rc" ]]; then
    if ! grep -qF '.local/bin' "$shell_rc" 2>/dev/null; then
      log "Adding ~/.local/bin to PATH in $shell_rc"
      echo "" >> "$shell_rc"
      echo "# Added by Charon installer" >> "$shell_rc"
      echo "$line" >> "$shell_rc"
    fi
    # Also export for the current session
    export PATH="$HOME/.local/bin:$PATH"
  else
    log 'Add ~/.local/bin to your shell PATH:'
    echo "$line"
  fi
}

install_homebrew() {
  if cmd_exists brew; then
    return
  fi
  log "Homebrew not found. Installing Homebrew..."
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
}

bootstrap_macos() {
  log "Detected macOS"

  if ! xcode-select -p >/dev/null 2>&1; then
    log "Installing Xcode Command Line Tools..."
    xcode-select --install || true
    log "If macOS shows a GUI installer prompt, complete it, then re-run this script if needed."
  fi

  install_homebrew
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi

  local pkgs=(python rust uv)
  if [[ "$INSTALL_TMUX" -eq 1 ]]; then
    pkgs+=(tmux)
  fi
  log "Installing packages with Homebrew: ${pkgs[*]}"
  brew install "${pkgs[@]}"
}

bootstrap_ubuntu() {
  log "Detected Ubuntu"
  if ! cmd_exists apt-get; then
    echo "Error: apt-get not found; unsupported Linux distro for this bootstrap script." >&2
    exit 1
  fi

  log "Updating apt package index..."
  need_sudo apt-get update

  local apt_pkgs=(
    curl
    ca-certificates
    git
    python3
    python3-pip
    python3-venv
    build-essential
    pkg-config
    libssl-dev
  )
  if [[ "$INSTALL_TMUX" -eq 1 ]]; then
    apt_pkgs+=(tmux)
  fi
  log "Installing apt packages: ${apt_pkgs[*]}"
  need_sudo apt-get install -y "${apt_pkgs[@]}"

  if ! cmd_exists cargo; then
    log "Installing Rust toolchain via rustup..."
    curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs | sh -s -- -y
    export PATH="$HOME/.cargo/bin:$PATH"
  fi

  if ! cmd_exists uv; then
    log "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
}

run_install_dev() {
  local args=()
  if [[ "$INSTALL_PLAYWRIGHT" -eq 0 ]]; then
    args+=(--no-playwright)
  else
    args+=(--playwright)
  fi
  if [[ "$FORCE" -eq 1 ]]; then
    args+=(--force)
  fi

  log "Running project installer: ./scripts/install-dev.sh ${args[*]}"
  "$INSTALL_DEV" "${args[@]}"
}

OS="$(uname -s)"
case "$OS" in
  Darwin)
    bootstrap_macos
    ;;
  Linux)
    if [[ -f /etc/os-release ]] && grep -qi 'ubuntu' /etc/os-release; then
      bootstrap_ubuntu
    else
      echo "Error: unsupported Linux distro. This bootstrap script currently supports Ubuntu only." >&2
      exit 1
    fi
    ;;
  *)
    echo "Error: unsupported OS: $OS" >&2
    exit 1
    ;;
esac

run_install_dev

log "Bootstrap complete."
ensure_path
log "Run: charon"
