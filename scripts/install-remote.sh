#!/usr/bin/env bash
set -euo pipefail

REPO="DanielSuncost/charon"
REF="master"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

usage() {
  cat <<EOF
Charon remote installer bootstrap

This script is intended to be curlable. It downloads Charon's release
installer from GitHub and runs it locally.

Usage:
  ./scripts/install-remote.sh [options passed through]

Examples:
  ./scripts/install-remote.sh
  ./scripts/install-remote.sh --version v0.1.0
  ./scripts/install-remote.sh --no-playwright

Curlable form:
  curl -fsSL https://raw.githubusercontent.com/${REPO}/${REF}/scripts/install-remote.sh | bash
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

RAW_URL="https://raw.githubusercontent.com/${REPO}/${REF}/scripts/install-release.sh"
LOCAL_SCRIPT="${TMP_DIR}/install-release.sh"

if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$RAW_URL" -o "$LOCAL_SCRIPT"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$LOCAL_SCRIPT" "$RAW_URL"
else
  echo "Error: curl or wget is required" >&2
  exit 1
fi

chmod +x "$LOCAL_SCRIPT"
exec bash "$LOCAL_SCRIPT" "$@"
