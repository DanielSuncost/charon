#!/bin/sh
# Install Charon inside an arbitrary benchmark/task container.
#
# Offline-first: when a bundle built by scripts/container/build_bundle.py is
# mounted (default /charon-bundle), nothing is fetched from the network — no
# apt, no curl, no PyPI, no GitHub. The bundle carries a standalone CPython
# 3.12, every wheel Charon needs and a git-archive snapshot of the source.
#
# Without a bundle it falls back to the network path (uv + PyPI) with retries,
# after a reachability preflight, so a dead network fails with the host named.
#
# Every failure ends with one machine-greppable line on stderr:
#   CHARON_BOOTSTRAP_FAIL cause=<cause> detail=<text>
# and a distinct exit code (see fail_* below). Success ends with
#   CHARON_BOOTSTRAP_OK agent_python=<path> mode=<offline|online>
#
# The agent runtime lives in $CHARON_VENV (default /charon-venv) and is never
# put first on PATH here: the task's own interpreter must stay the task's. The
# script reports both so a log shows which is which.
#
# POSIX sh on purpose: some task images have no bash.
set -u

BUNDLE=${CHARON_BUNDLE:-/charon-bundle}
VENV=${CHARON_VENV:-/charon-venv}
PYROOT=${CHARON_PYTHON_ROOT:-/opt/charon-python}
RETRIES=${CHARON_BOOTSTRAP_RETRIES:-4}

log() { printf 'charon-bootstrap: %s\n' "$*" >&2; }

fail() { # fail <exit-code> <cause> <detail>
  printf 'CHARON_BOOTSTRAP_FAIL cause=%s detail=%s\n' "$2" "$3" >&2
  exit "$1"
}

# Retry a command with exponential backoff (2, 4, 8 ... s). Returns the last
# exit status. Only used for network-touching steps.
retry() {
  n=1 delay=2
  while :; do
    "$@" && return 0
    rc=$?
    [ "$n" -ge "$RETRIES" ] && return "$rc"
    log "attempt $n/$RETRIES failed (rc=$rc): $*; retrying in ${delay}s"
    sleep "$delay"
    n=$((n + 1)) delay=$((delay * 2))
  done
}

# ── Platform ─────────────────────────────────────────────────────────────────
case "$(uname -m)" in
  x86_64|amd64) ARCH=x86_64 ;;
  aarch64|arm64) ARCH=aarch64 ;;
  *) fail 10 unsupported-arch "uname -m=$(uname -m); bundle has x86_64 and aarch64 only" ;;
esac

# The standalone CPython build is linked against glibc. On musl (Alpine) it
# would fail later with a baffling 'not found' from the dynamic loader.
if [ -e /lib/ld-musl-x86_64.so.1 ] || [ -e /lib/ld-musl-aarch64.so.1 ]; then
  fail 11 musl-libc "image uses musl libc (Alpine?); bundled CPython needs glibc"
fi

for tool in tar gzip; do
  command -v "$tool" >/dev/null 2>&1 || fail 12 missing-tool "'$tool' not in image; needed to unpack the bundled Python"
done

# ── Offline path ─────────────────────────────────────────────────────────────
install_offline() {
  manifest="$BUNDLE/MANIFEST"
  [ -r "$manifest" ] || fail 20 bundle-incomplete "$manifest missing"
  pytar=$(ls "$BUNDLE/python/$ARCH"/cpython-*.tar.gz 2>/dev/null | head -n 1)
  [ -n "$pytar" ] || fail 21 bundle-incomplete "no CPython tarball for $ARCH under $BUNDLE/python/$ARCH"
  [ -d "$BUNDLE/wheels/$ARCH" ] || fail 22 bundle-incomplete "no wheels for $ARCH under $BUNDLE/wheels/$ARCH"

  log "offline install from $BUNDLE ($(sed -n 's/^charon_commit=//p' "$manifest"), $ARCH)"
  mkdir -p "$PYROOT" || fail 30 not-writable "cannot create $PYROOT"
  # install_only tarballs unpack to ./python/
  tar -xzf "$pytar" -C "$PYROOT" || fail 31 unpack-failed "tar -xzf $pytar (disk full or corrupt bundle?)"
  base="$PYROOT/python/bin/python3"
  "$base" -c 'import sys; assert sys.version_info >= (3, 12)' 2>/dev/null \
    || fail 32 python-broken "bundled $base does not run (glibc too old? $(ldd --version 2>&1 | head -n 1))"

  "$base" -m venv "$VENV" || fail 33 venv-failed "$base -m venv $VENV"
  "$VENV/bin/python" -m pip install --quiet --no-index --disable-pip-version-check \
      --find-links "$BUNDLE/wheels/$ARCH" charon \
    || fail 34 wheel-install-failed "pip --no-index from $BUNDLE/wheels (bundle built for a different commit or arch?)"
  MODE=offline
}

# ── Online fallback ──────────────────────────────────────────────────────────
# Name the first unreachable host instead of letting pip/uv time out opaquely.
preflight_host() {
  host=$1
  if command -v getent >/dev/null 2>&1; then
    getent hosts "$host" >/dev/null 2>&1 || return 1
  fi
  return 0
}

install_online() {
  log "no bundle at $BUNDLE; falling back to network install"
  [ -d /charon ] || fail 40 no-source "neither $BUNDLE nor /charon is mounted"
  for host in astral.sh releases.astral.sh pypi.org files.pythonhosted.org; do
    retry preflight_host "$host" || fail 41 dns-unreachable "cannot resolve $host from inside the container (docker DNS / network down)"
  done
  fetch=""
  if command -v curl >/dev/null 2>&1; then
    fetch="curl -fsSL --retry 5 --retry-all-errors --connect-timeout 20"
  elif command -v wget >/dev/null 2>&1; then
    fetch="wget -q -T 20 -t 5 -O -"
  else
    fail 42 missing-tool "neither curl nor wget in image and no bundle mounted; mount a bundle (scripts/container/build_bundle.py)"
  fi
  uvdir=/opt/charon-uv-bin
  installer=$(mktemp)
  retry sh -c "$fetch https://astral.sh/uv/install.sh > '$installer'" \
    || fail 43 download-failed "uv installer from astral.sh"
  env UV_INSTALL_DIR=$uvdir sh "$installer" >/dev/null || fail 44 install-failed "uv installer"
  export UV_PYTHON_INSTALL_DIR="$PYROOT" UV_HTTP_TIMEOUT=60
  retry "$uvdir/uv" python install 3.12 || fail 45 download-failed "CPython 3.12 from releases.astral.sh"
  "$uvdir/uv" venv --quiet --seed --python 3.12 "$VENV" || fail 33 venv-failed "uv venv $VENV"
  src=$(mktemp -d)
  cp -a /charon/. "$src/" || fail 46 copy-failed "/charon -> $src (disk full?)"
  retry "$uvdir/uv" pip install --quiet --python "$VENV/bin/python" "$src" \
    || fail 47 download-failed "Charon dependencies from PyPI"
  MODE=online
}

if [ -d "$BUNDLE" ]; then install_offline; else install_online; fi

# ── Verify the agent runtime, report both runtimes ──────────────────────────
"$VENV/bin/python" - <<'EOF' || fail 50 import-failed "charon/httpx not importable from $VENV/bin/python"
import sys, httpx, charon
assert sys.prefix != sys.base_prefix, "not running inside the venv"
assert sys.version_info >= (3, 12), sys.version
EOF
log "agent runtime: $VENV/bin/python ($("$VENV/bin/python" -V 2>&1)) [not on PATH; invoke by path]"
task_py=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)
if [ -n "$task_py" ]; then
  log "task runtime:  $task_py ($("$task_py" -V 2>&1)) [first on PATH; the task's, not Charon's]"
else
  log "task runtime:  no python on PATH"
fi
printf 'CHARON_BOOTSTRAP_OK agent_python=%s mode=%s\n' "$VENV/bin/python" "$MODE"
