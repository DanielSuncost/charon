#!/usr/bin/env python3
"""Build an offline Charon install bundle for benchmark/task containers.

    uv run --no-project --python 3.12 scripts/container/build_bundle.py OUT_DIR

Mount OUT_DIR read-only at /charon-bundle (and OUT_DIR/src at /charon for
adapters that run scripts/charon_chat.py from there), then run
scripts/container/charon-bootstrap.sh inside the container. The install then
needs no network: no apt, no curl, no PyPI, no GitHub.

Layout:
    OUT_DIR/MANIFEST                      commit, sources, sha256 of every file
    OUT_DIR/src/                          `git archive HEAD` — committed files only,
                                          so .charon_state/ (OAuth tokens), .venv/
                                          and local edits can never leak in
    OUT_DIR/python/<arch>/cpython-*.tar.gz  standalone CPython 3.12 (glibc)
    OUT_DIR/wheels/<arch>/*.whl           charon + its runtime dependencies
    OUT_DIR/charon-bootstrap.sh           copy of the container-side installer

Needs `git` and `uv` on the host; downloads ~70 MB for both arches.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ARCHES = ('x86_64', 'aarch64')


def run(*cmd: str, cwd: Path | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def python_url(arch: str, minor: str) -> tuple[str, str]:
    """Newest standalone CPython <minor> for linux-<arch>-gnu, as uv knows it."""
    listing = json.loads(run('uv', 'python', 'list', '--all-platforms', '--all-versions',
                             '--output-format', 'json'))
    best = None
    for p in listing:
        if (p.get('implementation') == 'cpython' and p.get('os') == 'linux'
                and p.get('libc') == 'gnu' and p.get('arch') == arch
                and p.get('variant', 'default') == 'default' and p.get('url')
                and p['version'].startswith(minor + '.')
                and p['version'].replace('.', '').isdigit()):
            key = tuple(int(x) for x in p['version'].split('.'))
            if best is None or key > best[0]:
                best = (key, p['version'], p['url'])
    if best is None:
        sys.exit(f'build_bundle: uv knows no CPython {minor} build for linux-{arch}-gnu')
    return best[1], best[2]


def download(url: str, dest: Path, attempts: int = 4) -> None:
    for n in range(1, attempts + 1):
        try:
            tmp = dest.with_suffix(dest.suffix + '.part')
            with urllib.request.urlopen(url, timeout=60) as r, tmp.open('wb') as f:
                shutil.copyfileobj(r, f)
            tmp.rename(dest)
            return
        except OSError as e:
            if n == attempts:
                sys.exit(f'build_bundle: download failed after {attempts} attempts: {url}: {e}')
            print(f'  retry {n}/{attempts} for {url}: {e}', file=sys.stderr)
            time.sleep(2 ** n)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('out', type=Path)
    ap.add_argument('--arch', action='append', choices=ARCHES,
                    help='repeatable; default both')
    ap.add_argument('--python', default='3.12')
    args = ap.parse_args()
    arches = args.arch or list(ARCHES)
    out: Path = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        sys.exit(f'build_bundle: {out} exists and is not empty; pick a fresh directory')
    out.mkdir(parents=True, exist_ok=True)

    commit = run('git', 'rev-parse', 'HEAD', cwd=REPO).strip()
    dirty = bool(run('git', 'status', '--porcelain', '--untracked-files=no', cwd=REPO).strip())
    if dirty:
        print('build_bundle: WARNING working tree has uncommitted changes; '
              'the bundle contains HEAD only', file=sys.stderr)

    print(f'source: git archive {commit[:12]}')
    src = out / 'src'
    src.mkdir()
    archive = subprocess.run(['git', 'archive', 'HEAD'], cwd=REPO, check=True,
                             capture_output=True).stdout
    tar_path = out / '.src.tar'
    tar_path.write_bytes(archive)
    with tarfile.open(tar_path) as t:
        t.extractall(src, filter='data')
    tar_path.unlink()

    print('wheel: charon')
    any_dir = out / '.wheel-build'
    run('uv', 'build', '--wheel', '--quiet', '--out-dir', str(any_dir), str(src))
    charon_whl = next(any_dir.glob('charon-*.whl'))

    manifest: list[str] = [f'charon_commit={commit}', f'source_dirty={dirty}',
                           f'built_at={time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}']
    for arch in arches:
        version, url = python_url(arch, args.python)
        print(f'python {version} {arch}')
        pydir = out / 'python' / arch
        pydir.mkdir(parents=True)
        dest = pydir / f'cpython-{version}-{arch}-linux-gnu.tar.gz'
        download(url, dest)
        manifest.append(f'python_{arch}={version} {url}')

        print(f'wheels {arch}')
        whdir = out / 'wheels' / arch
        # pip download resolves charon's own dependency list from the wheel
        # metadata, so the bundle cannot drift from pyproject.toml.
        run('uvx', '--quiet', '--from', 'pip', 'pip', 'download', '--quiet',
            '--disable-pip-version-check', '--only-binary=:all:',
            '--implementation', 'cp', '--python-version', args.python,
            '--platform', f'manylinux_2_17_{arch}', '--platform', f'manylinux2014_{arch}',
            '--dest', str(whdir), str(charon_whl))
    shutil.rmtree(any_dir)

    shutil.copy2(REPO / 'scripts' / 'container' / 'charon-bootstrap.sh',
                 out / 'charon-bootstrap.sh')

    for f in sorted(p for p in out.rglob('*') if p.is_file() and 'src' not in p.relative_to(out).parts[:1]):
        manifest.append(f'sha256 {sha256(f)} {f.relative_to(out)}')
    (out / 'MANIFEST').write_text('\n'.join(manifest) + '\n')
    size = sum(p.stat().st_size for p in out.rglob('*') if p.is_file())
    print(f'bundle: {out} ({size / 2**20:.1f} MiB, commit {commit[:12]})')


if __name__ == '__main__':
    main()
