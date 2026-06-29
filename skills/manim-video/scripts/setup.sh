#!/usr/bin/env bash
# Manim Video Skill — dependency installer / checker for Charon.
#
#   setup.sh              install + verify deps (default)
#   setup.sh --check      verify only, install nothing
#   setup.sh --with-latex also install LaTeX (needed only for MathTex equations)
#
# Targets Charon's project-local .venv and supports macOS (Homebrew) + Ubuntu (apt).
set -euo pipefail

G="\033[0;32m"; R="\033[0;31m"; Y="\033[0;33m"; N="\033[0m"
ok()   { echo -e "  ${G}+${N} $1"; }
fail() { echo -e "  ${R}x${N} $1"; }
warn() { echo -e "  ${Y}!${N} $1"; }

CHECK_ONLY=0
WITH_LATEX=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --with-latex) WITH_LATEX=1 ;;
    -h|--help)
      sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

# Resolve Charon root (skills/manim-video/scripts/setup.sh -> repo root) and venv.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
VENV_DIR="${CHARON_VENV:-$ROOT/.venv}"
VENV_PY="$VENV_DIR/bin/python3"
[ -x "$VENV_PY" ] || VENV_PY="$(command -v python3 || true)"

OS="$(uname -s)"
echo ""
echo "Manim Video Skill — $([ "$CHECK_ONLY" -eq 1 ] && echo 'check' || echo 'setup') ($OS)"
echo "  venv: $VENV_DIR"
echo ""

# ── installers ───────────────────────────────────────────────────────
brew_install() { brew list "$1" >/dev/null 2>&1 || brew install "$1"; }
apt_install()  { sudo apt-get install -y "$@"; }

install_system_deps() {
  if [ "$OS" = "Darwin" ]; then
    if ! command -v brew >/dev/null 2>&1; then
      fail "Homebrew not found — install from https://brew.sh, then re-run."; return 1
    fi
    for pkg in cairo pango pkg-config ffmpeg; do
      echo "  installing $pkg ..."; brew_install "$pkg" >/dev/null 2>&1 || warn "brew install $pkg had issues"
    done
    [ "$WITH_LATEX" -eq 1 ] && { echo "  installing mactex-no-gui (large) ..."; brew install --cask mactex-no-gui >/dev/null 2>&1 || warn "mactex install had issues"; }
  elif [ "$OS" = "Linux" ]; then
    echo "  apt-get update ..."; sudo apt-get update -qq || true
    apt_install libcairo2-dev libpango1.0-dev pkg-config ffmpeg python3-dev || warn "some apt packages had issues"
    [ "$WITH_LATEX" -eq 1 ] && { echo "  installing texlive (large) ..."; apt_install texlive texlive-latex-extra dvisvgm || warn "texlive install had issues"; }
  else
    warn "Unsupported OS '$OS' — install cairo, pango, pkg-config, ffmpeg manually."
  fi
}

install_manim() {
  echo "  installing manim into venv ..."
  if command -v uv >/dev/null 2>&1 && [ -d "$VENV_DIR" ]; then
    VIRTUAL_ENV="$VENV_DIR" uv pip install --python "$VENV_PY" "manim>=0.20" >/dev/null 2>&1 \
      || "$VENV_PY" -m pip install "manim>=0.20"
  else
    "$VENV_PY" -m pip install "manim>=0.20"
  fi
}

if [ "$CHECK_ONLY" -eq 0 ]; then
  install_system_deps || true
  install_manim || warn "manim install had issues — see output above"
  echo ""
fi

# ── verification ─────────────────────────────────────────────────────
errors=0
"$VENV_PY" --version >/dev/null 2>&1 && ok "Python $($VENV_PY --version 2>&1 | awk '{print $2}') ($VENV_PY)" || { fail "Python 3 not found"; errors=$((errors+1)); }
"$VENV_PY" -c "import manim" 2>/dev/null && ok "Manim $($VENV_PY -c 'import manim,sys; sys.stdout.write(manim.__version__)' 2>/dev/null)" || { fail "Manim not importable in venv — run: bash skills/manim-video/scripts/setup.sh"; errors=$((errors+1)); }
command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg" || { fail "ffmpeg not on PATH (macOS: brew install ffmpeg)"; errors=$((errors+1)); }
if command -v pdflatex >/dev/null 2>&1; then ok "LaTeX (pdflatex) — MathTex available"; else warn "LaTeX not found — MathTex/equations disabled (optional: --with-latex)"; fi

echo ""
if [ "$errors" -eq 0 ]; then
  echo -e "${G}All required prerequisites satisfied. Ready to render.${N}"
else
  echo -e "${R}$errors required prerequisite(s) missing.${N}"
  [ "$CHECK_ONLY" -eq 1 ] && echo "Run without --check to install them."
  exit 1
fi
echo ""
