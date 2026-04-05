#!/usr/bin/env bash
set -euo pipefail

REPO="DanielSuncost/charon"
API_ROOT="https://api.github.com/repos/${REPO}"
DOWNLOAD_ROOT="https://github.com/${REPO}/releases/download"
INSTALL_ROOT="${HOME}/.charon"
BIN_DIR="${HOME}/.local/bin"
PLAYWRIGHT=1
VERSION="latest"
FORCE=0

usage() {
  cat <<EOF
Charon release installer

Installs a published Charon release bundle from GitHub Releases into:
  ${INSTALL_ROOT}

Usage:
  ./scripts/install-release.sh [options]

Options:
  --version <tag>     Install a specific tag (example: v0.1.0)
  --latest            Install the latest release (default)
  --no-playwright     Skip Playwright + Chromium install
  --playwright        Force Playwright + Chromium install
  --force             Reinstall even if target version already exists
  -h, --help          Show this help
EOF
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required command not found: $1" >&2
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      VERSION="${2:-}"
      if [[ -z "$VERSION" ]]; then
        echo "Error: --version requires a tag" >&2
        exit 1
      fi
      shift 2
      ;;
    --latest)
      VERSION="latest"
      shift
      ;;
    --no-playwright)
      PLAYWRIGHT=0
      shift
      ;;
    --playwright)
      PLAYWRIGHT=1
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

need_cmd python3
need_cmd cargo

HTTP_GET() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$1"
  else
    echo "Error: curl or wget is required" >&2
    exit 1
  fi
}

DOWNLOAD_FILE() {
  local url="$1"
  local out="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fL "$url" -o "$out"
  else
    wget -O "$out" "$url"
  fi
}

platform_asset() {
  local os arch
  os="$(uname -s)"
  arch="$(uname -m)"
  case "$os" in
    Linux) os="linux" ;;
    Darwin) os="macos" ;;
    *) echo "unsupported-os"; return ;;
  esac
  case "$arch" in
    x86_64|amd64) arch="x86_64" ;;
    arm64|aarch64)
      if [[ "$os" == "macos" ]]; then
        arch="aarch64"
      else
        arch="arm64"
      fi
      ;;
    *) echo "unsupported-arch"; return ;;
  esac
  echo "charon-${os}-${arch}"
}

resolve_version() {
  if [[ "$VERSION" != "latest" ]]; then
    printf '%s' "$VERSION"
    return
  fi
  HTTP_GET "${API_ROOT}/releases/latest" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])'
}

TAG="$(resolve_version)"
ASSET_BASE="$(platform_asset)"
if [[ "$ASSET_BASE" == unsupported-* ]]; then
  echo "Error: this platform is not currently supported by the release pipeline: $(uname -s) $(uname -m)" >&2
  echo "Supported right now: linux x86_64, macOS arm64" >&2
  exit 1
fi
ASSET_FILE="${ASSET_BASE}.tar.gz"
URL="${DOWNLOAD_ROOT}/${TAG}/${ASSET_FILE}"
TARGET_DIR="${INSTALL_ROOT}/versions/${TAG}"
CURRENT_LINK="${INSTALL_ROOT}/current"
LAUNCHER_LINK="${BIN_DIR}/charon"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "${INSTALL_ROOT}/versions" "$BIN_DIR"

if [[ -d "$TARGET_DIR" && "$FORCE" -ne 1 ]]; then
  echo "Version ${TAG} is already installed at ${TARGET_DIR}"
else
  echo "Downloading ${URL} ..."
  ARCHIVE_PATH="${TMP_DIR}/${ASSET_FILE}"
  DOWNLOAD_FILE "$URL" "$ARCHIVE_PATH"

  echo "Extracting release bundle..."
  tar -xzf "$ARCHIVE_PATH" -C "$TMP_DIR"
  EXTRACTED_DIR="${TMP_DIR}/${ASSET_BASE}"
  if [[ ! -d "$EXTRACTED_DIR" ]]; then
    echo "Error: extracted bundle missing expected directory ${ASSET_BASE}" >&2
    exit 1
  fi

  rm -rf "$TARGET_DIR"
  mkdir -p "$(dirname "$TARGET_DIR")"
  mv "$EXTRACTED_DIR" "$TARGET_DIR"
fi

ln -sfn "$TARGET_DIR" "$CURRENT_LINK"
ln -sfn "${CURRENT_LINK}/charon" "$LAUNCHER_LINK"
chmod +x "${CURRENT_LINK}/charon" || true
chmod +x "${CURRENT_LINK}/bin/charon" || true
chmod +x "${CURRENT_LINK}/scripts/install-dev.sh" || true
chmod +x "${CURRENT_LINK}/scripts/install.sh" || true
chmod +x "${CURRENT_LINK}/scripts/install-release.sh" || true

if ! python3 -c "import httpx" >/dev/null 2>&1 || [[ "$FORCE" -eq 1 ]]; then
  echo "Installing Python dependencies from release bundle..."
  if command -v uv >/dev/null 2>&1; then
    uv pip install -r "${CURRENT_LINK}/requirements.txt"
  else
    python3 -m pip install -r "${CURRENT_LINK}/requirements.txt"
  fi
else
  echo "Python dependencies already available."
fi

if [[ "$PLAYWRIGHT" -eq 1 ]]; then
  echo "Ensuring Playwright + Chromium are installed..."
  if command -v uv >/dev/null 2>&1; then
    uv pip install playwright
  else
    python3 -m pip install playwright
  fi
  python3 -m playwright install chromium
fi

echo
echo "Installed Charon ${TAG} to ${TARGET_DIR}"
echo "Launcher: ${LAUNCHER_LINK}"
if [[ ":${PATH}:" != *":${BIN_DIR}:"* ]]; then
  echo "Note: ${BIN_DIR} is not on your PATH. Add this to your shell rc:"
  echo "  export PATH=\"${BIN_DIR}:\$PATH\""
fi

echo
echo "Run:"
echo "  charon"
