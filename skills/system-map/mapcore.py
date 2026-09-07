"""System Map — declared-map contract, source walk, validator, extractor.

A stdlib-only port of Acheron's ``src/overseer/map/{schema,walk,validate,extract}.js``.
The two implementations are contract-tested against the same fixture repo and must
produce the same errors, gaps, conflicts, and relations (see tests/test_system_map.py).

Shared by ``validate.py`` / ``extract.py`` / ``query.py`` (the skill's CLIs) and by
``charon.tools.system_map_tool`` (in-process).  Keep it dependency-free.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# ── contract (schema.js) ──────────────────────────────────────────────────────

MAP_VERSION = 1
KINDS = ['module', 'service', 'process', 'library', 'datastore', 'interface', 'job', 'ui']
INTERFACE_KINDS = ['http', 'ws', 'unix-socket', 'tcp', 'ssh', 'cli', 'file', 'event', 'library', 'custom']
SLUG_RE = re.compile(r'^[a-z][a-z0-9._-]*$')
DEFAULT_MAP_FILE = 'system-map.json'
EXTRACTOR = {'name': 'system-map', 'version': '1'}

_COMPONENT_KEYS = {'id', 'subsystem', 'name', 'kind', 'description', 'code', 'entrypoints', 'interfaces', 'depends_on', 'owner_role', 'docs', 'invariants'}
_SUBSYSTEM_KEYS = {'id', 'name', 'description'}
_EXTERNAL_KEYS = {'id', 'name', 'kind', 'description', 'packages'}
_INTERFACE_KEYS = {'kind', 'locator', 'protocol', 'description'}
_TOP_KEYS = {'version', 'system', 'subsystems', 'components', 'externals'}


def _is_obj(v) -> bool:
    return isinstance(v, dict)


def _slug_ok(v) -> bool:
    return isinstance(v, str) and bool(SLUG_RE.match(v))


def check_shape(m) -> list[str]:
    """Structural check of a declared map. Returns error strings (empty = ok)."""
    errors: list[str] = []
    err = errors.append
    if not _is_obj(m):
        return ['map must be an object']
    for k in m:
        if k not in _TOP_KEYS:
            err(f'unknown top-level key "{k}"')
    if m.get('version') != MAP_VERSION or isinstance(m.get('version'), bool):
        err(f'version must be {MAP_VERSION}')
    system = m.get('system')
    if not _is_obj(system):
        err('system is required')
    else:
        if not _slug_ok(str(system.get('id') or '')):
            err('system.id must be a slug')
        if not system.get('name'):
            err('system.name is required')

    def str_arr(v, where, item_check=None):
        if v is None:
            return
        if not isinstance(v, list):
            err(f'{where} must be an array')
            return
        for i, x in enumerate(v):
            if not isinstance(x, str) or not x:
                err(f'{where}[{i}] must be a non-empty string')
            elif item_check:
                item_check(x, i)

    subsystems = m.get('subsystems')
    if not isinstance(subsystems, list):
        err('subsystems must be an array')
    else:
        for i, s in enumerate(subsystems):
            w = f'subsystems[{i}]'
            if not _is_obj(s):
                err(f'{w} must be an object')
                continue
            for k in s:
                if k not in _SUBSYSTEM_KEYS:
                    err(f'{w}: unknown key "{k}"')
            if not _slug_ok(str(s.get('id') or '')):
                err(f'{w}.id must be a slug')
            if not s.get('name'):
                err(f'{w}.name is required')
    components = m.get('components')
    if not isinstance(components, list):
        err('components must be an array')
    else:
        for i, c in enumerate(components):
            w = f'components[{i}]' + (f' ({c["id"]})' if _is_obj(c) and c.get('id') else '')
            if not _is_obj(c):
                err(f'{w} must be an object')
                continue
            for k in c:
                if k not in _COMPONENT_KEYS:
                    err(f'{w}: unknown key "{k}"')
            if not _slug_ok(str(c.get('id') or '')):
                err(f'{w}.id must be a slug')
            if not _slug_ok(str(c.get('subsystem') or '')):
                err(f'{w}.subsystem must be a slug')
            if not c.get('name'):
                err(f'{w}.name is required')
            if c.get('kind') not in KINDS:
                err(f'{w}.kind must be one of {"|".join(KINDS)}')
            if not isinstance(c.get('code'), list):
                err(f'{w}.code must be an array')
            else:
                str_arr(c.get('code'), f'{w}.code')
            str_arr(c.get('entrypoints'), f'{w}.entrypoints')
            str_arr(c.get('depends_on'), f'{w}.depends_on',
                    lambda x, j, w=w: None if SLUG_RE.match(x) else err(f'{w}.depends_on[{j}] must be a slug'))
            dep = c.get('depends_on')
            if isinstance(dep, list) and len(set(dep)) != len(dep):
                err(f'{w}.depends_on has duplicates')
            str_arr(c.get('docs'), f'{w}.docs')
            str_arr(c.get('invariants'), f'{w}.invariants')
            if 'owner_role' in c and not isinstance(c.get('owner_role'), str):
                err(f'{w}.owner_role must be a string')
            if 'interfaces' in c:
                ifs = c.get('interfaces')
                if not isinstance(ifs, list):
                    err(f'{w}.interfaces must be an array')
                else:
                    for j, it in enumerate(ifs):
                        iw = f'{w}.interfaces[{j}]'
                        if not _is_obj(it):
                            err(f'{iw} must be an object')
                            continue
                        for k in it:
                            if k not in _INTERFACE_KEYS:
                                err(f'{iw}: unknown key "{k}"')
                        if it.get('kind') not in INTERFACE_KINDS:
                            err(f'{iw}.kind must be one of {"|".join(INTERFACE_KINDS)}')
                        if not it.get('locator'):
                            err(f'{iw}.locator is required')
    if 'externals' in m:
        exts = m.get('externals')
        if not isinstance(exts, list):
            err('externals must be an array')
        else:
            for i, e in enumerate(exts):
                w = f'externals[{i}]'
                if not _is_obj(e):
                    err(f'{w} must be an object')
                    continue
                for k in e:
                    if k not in _EXTERNAL_KEYS:
                        err(f'{w}: unknown key "{k}"')
                if not _slug_ok(str(e.get('id') or '')):
                    err(f'{w}.id must be a slug')
                if not e.get('name'):
                    err(f'{w}.name is required')
                if 'kind' in e and e.get('kind') not in KINDS:
                    err(f'{w}.kind must be one of {"|".join(KINDS)}')
                str_arr(e.get('packages'), f'{w}.packages')
    return errors


def normalize_path(p) -> str:
    s = str(p).replace('\\', '/')
    if s.startswith('./'):
        s = s[2:]
    s = re.sub(r'/+', '/', s)
    if s.endswith('/'):
        s = s[:-1]
    return s


def parse_code_ref(ref) -> dict:
    """Split a code ref into its glob/path part and optional ``#symbol`` anchor."""
    s = str(ref)
    i = s.find('#')
    if i < 0:
        return {'path': normalize_path(s), 'symbol': None, 'anchored': False}
    return {'path': normalize_path(s[:i]), 'symbol': s[i + 1:].strip(), 'anchored': True}


def is_glob(p) -> bool:
    return re.search(r'[*?{\[]', p) is not None


def _escape(s: str) -> str:
    return re.sub(r'[.+^$()|\[\]\\]', lambda m: '\\' + m.group(0), s)


def glob_to_regexp(pattern) -> re.Pattern:
    """Minimal glob → regex: ``**`` spans segments (incl. none), ``*`` within a
    segment, ``?`` one non-``/`` char, ``{a,b}`` alternation. Whole-path match."""
    p = normalize_path(pattern)
    out = ''
    i = 0
    n = len(p)
    while i < n:
        c = p[i]
        if c == '*':
            if i + 1 < n and p[i + 1] == '*':
                if i + 2 < n and p[i + 2] == '/':
                    out += '(?:[^/]+/)*'
                    i += 3
                    continue
                out += '.*'
                i += 2
                continue
            out += '[^/]*'
        elif c == '?':
            out += '[^/]'
        elif c == '{':
            j = p.find('}', i)
            if j < 0:
                out += '\\{'
            else:
                out += '(?:' + '|'.join(_escape(x) for x in p[i + 1:j].split(',')) + ')'
                i = j
        else:
            out += _escape(c)
        i += 1
    return re.compile('^' + out + '$')


def match_glob(pattern, path) -> bool:
    return glob_to_regexp(pattern).match(normalize_path(path)) is not None


# ── source walk (walk.js) ─────────────────────────────────────────────────────

DEFAULT_IGNORE_DIRS = {
    '.git', 'node_modules', 'target', 'dist', 'build', 'DerivedData', '__pycache__', '.pytest_cache',
    '.venv', 'venv', '.idea', '.vscode', '.claude', 'coverage', '.cache', '.next', 'out',
}
DEFAULT_IGNORE_DIR_SUFFIXES = ['.xcodeproj', '.xcworkspace', '.app', '.framework']
DEFAULT_IGNORE_FILES = {'.git', '.DS_Store', 'pnpm-lock.yaml', 'package-lock.json', 'yarn.lock', 'Cargo.lock', 'Podfile.lock', 'poetry.lock', 'uv.lock'}
BINARY_EXTS = {
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'ico', 'icns', 'pdf', 'zip', 'tar', 'gz', 'xz', 'bz2', '7z', 'dmg',
    'woff', 'woff2', 'ttf', 'otf', 'mp4', 'mov', 'mp3', 'wav', 'p8', 'db', 'sqlite', 'sqlite3', 'lock',
    'jar', 'class', 'o', 'so', 'dylib', 'a', 'wasm', 'pyc', 'bin', 'exe',
}
MAX_FILE_BYTES = 4 * 1024 * 1024


def parse_gitignore(text) -> list[dict]:
    """Parse the simple subset of .gitignore we honor (no negations)."""
    rules = []
    for raw in str(text or '').split('\n'):
        line = raw.strip()
        if not line or line.startswith('#') or line.startswith('!'):
            continue
        dir_only = line.endswith('/')
        pat = normalize_path(re.sub(r'/$', '', line))
        anchored = '/' in pat
        if pat.startswith('/'):
            pat = pat[1:]
        rx = glob_to_regexp(pat) if anchored else glob_to_regexp(f'**/{pat}')
        rules.append({'re': rx, 'dir_only': dir_only, 'pat': pat})
    return rules


def _ignored_by_rules(rules, rel: str, is_dir: bool) -> bool:
    parts = rel.split('/')
    for r in rules:
        rx = r['re']
        if r['dir_only'] and not is_dir:
            # `dir/` rules also hide everything under that dir
            if rx.match('/'.join(parts[:-1])) or any(rx.match('/'.join(parts[:i + 1])) for i in range(len(parts))):
                return True
            continue
        if rx.match(rel):
            return True
    return False


def walk_source_files(root, ignore=None, gitignore=True) -> list[str]:
    """List source files under ``root`` as ``/``-separated relative paths (sorted)."""
    root = str(root)
    extra = set(ignore or [])
    rules = []
    gi = os.path.join(root, '.gitignore')
    if gitignore and os.path.isfile(gi):
        rules = parse_gitignore(read_text(root, '.gitignore') or '')
    out: list[str] = []

    def visit(d: str, rel: str):
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            name = e.name
            rel_path = f'{rel}/{name}' if rel else name
            if e.is_symlink():
                continue
            if e.is_dir(follow_symlinks=False):
                if name in DEFAULT_IGNORE_DIRS or name in extra or rel_path in extra:
                    continue
                if any(name.endswith(s) for s in DEFAULT_IGNORE_DIR_SUFFIXES):
                    continue
                if _ignored_by_rules(rules, rel_path, True):
                    continue
                visit(os.path.join(d, name), rel_path)
            elif e.is_file(follow_symlinks=False):
                if name in DEFAULT_IGNORE_FILES or rel_path in extra:
                    continue
                ext = name[name.rfind('.') + 1:].lower() if '.' in name else ''
                if ext in BINARY_EXTS:
                    continue
                if _ignored_by_rules(rules, rel_path, False):
                    continue
                try:
                    if e.stat(follow_symlinks=False).st_size > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                out.append(rel_path)

    visit(root, '')
    return sorted(out)


def read_text(root, rel) -> str | None:
    try:
        with open(os.path.join(str(root), rel), 'r', encoding='utf-8', errors='replace') as fh:
            return fh.read()
    except OSError:
        return None


def count_lines(text) -> int:
    if not text:
        return 0
    n = text.count('\n')
    return n if text.endswith('\n') else n + 1


# ── validator (validate.js) ───────────────────────────────────────────────────

def _js_round(x: float) -> int:
    """JS Math.round for non-negative numbers (half up)."""
    import math
    return int(math.floor(x + 0.5))


def validate_map(m, root=None, files=None, file_text=None, ignore=None) -> dict:
    """Validate a declared map against the code. Same rules and result shape as validate.js."""
    root = str(root or os.getcwd())
    errors: list[dict] = []
    warnings: list[dict] = []
    gaps: list[dict] = []

    def E(code, message, **extra):
        errors.append({'code': code, 'message': message, **extra})

    def W(code, message, **extra):
        warnings.append({'code': code, 'message': message, **extra})

    for msg in check_shape(m):
        E('schema', msg)
    if errors:
        return {'ok': False, 'errors': errors, 'warnings': warnings, 'gaps': gaps,
                'coverage': {'files': 0, 'mapped': 0, 'percent': 0}, 'owners': {}, 'anchors': []}

    subsystems = {s['id']: s for s in m['subsystems']}
    comps = m['components']
    externals = {e['id']: e for e in (m.get('externals') or [])}
    ids: set[str] = set()
    for s in m['subsystems']:
        if s['id'] in ids:
            E('duplicate_id', f'id "{s["id"]}" declared twice', id=s['id'])
        ids.add(s['id'])
    for c in comps:
        if c['id'] in ids:
            E('duplicate_id', f'id "{c["id"]}" declared twice', id=c['id'])
        ids.add(c['id'])
    for e in externals.values():
        if e['id'] in ids:
            E('duplicate_id', f'id "{e["id"]}" declared twice', id=e['id'])
        ids.add(e['id'])
    comp_ids = {c['id'] for c in comps}

    for c in comps:
        if c['subsystem'] not in subsystems:
            E('unknown_subsystem', f'component "{c["id"]}" belongs to unknown subsystem "{c["subsystem"]}"', component=c['id'])
        for d in c.get('depends_on') or []:
            if d == c['id']:
                E('self_dependency', f'component "{c["id"]}" depends on itself', component=c['id'])
            elif d not in comp_ids and d not in externals:
                E('unknown_dependency', f'component "{c["id"]}" depends on unknown "{d}"', component=c['id'], target=d)

    for cyc in find_cycles(comps):
        W('cycle', 'declared dependency cycle: ' + ' → '.join(cyc), cycle=cyc)

    file_list = [normalize_path(f) for f in files] if files is not None else walk_source_files(root, ignore=ignore)
    file_set = set(file_list)
    owners: dict[str, str] = {}
    claimed: dict[str, list[str]] = {}
    anchors: list[dict] = []
    ft = file_text or (lambda rel: read_text(root, rel))

    for c in comps:
        seen_patterns: set[str] = set()
        for ref in c.get('code') or []:
            pr = parse_code_ref(ref)
            path, symbol, anchored = pr['path'], pr['symbol'], pr['anchored']
            if anchored:
                if is_glob(path):
                    E('anchor_glob', f'component "{c["id"]}": anchor "{ref}" must name one file, not a glob', component=c['id'], ref=ref)
                    continue
                if path not in file_set and ft(path) is None:
                    E('anchor_file_missing', f'component "{c["id"]}": anchor file "{path}" not found', component=c['id'], ref=ref)
                    continue
                line = find_symbol(ft(path), symbol)
                if line is None:
                    E('anchor_unresolved', f'component "{c["id"]}": symbol "{symbol}" not found in {path}', component=c['id'], ref=ref)
                else:
                    anchors.append({'component': c['id'], 'file': path, 'symbol': symbol, 'line': line})
                continue
            if path in seen_patterns:
                W('duplicate_pattern', f'component "{c["id"]}" lists "{path}" twice', component=c['id'], ref=ref)
                continue
            seen_patterns.add(path)
            rx = glob_to_regexp(path) if is_glob(path) else None
            hits = 0
            for f in file_list:
                match = (rx.match(f) is not None) if rx else (f == path or f.startswith(path + '/'))
                if not match:
                    continue
                hits += 1
                claimed.setdefault(f, []).append(c['id'])
                if f not in owners:
                    owners[f] = c['id']
            if not hits:
                W('glob_no_match', f'component "{c["id"]}": "{path}" matches no source file', component=c['id'], ref=ref)
    for f, cs in claimed.items():
        uniq = list(dict.fromkeys(cs))
        if len(uniq) > 1:
            E('overlap', f'"{f}" is claimed by {", ".join(uniq)} — a file must be owned by exactly one component', file=f, components=uniq)
    for f in file_list:
        if f not in owners:
            gaps.append({'file': f, 'reason': 'unmapped'})

    for c in comps:
        for ref in c.get('entrypoints') or []:
            pr = parse_code_ref(ref)
            path, symbol, anchored = pr['path'], pr['symbol'], pr['anchored']
            if path not in file_set and ft(path) is None:
                W('entrypoint_missing', f'component "{c["id"]}": entrypoint "{ref}" not found', component=c['id'], ref=ref)
                continue
            if anchored and find_symbol(ft(path), symbol) is None:
                W('entrypoint_unresolved', f'component "{c["id"]}": entrypoint symbol "{symbol}" not found in {path}', component=c['id'], ref=ref)

    mapped = len(file_list) - len(gaps)
    coverage = {'files': len(file_list), 'mapped': mapped,
                'percent': _js_round(mapped / len(file_list) * 100) if file_list else 100}
    return {'ok': not errors, 'errors': errors, 'warnings': warnings, 'gaps': gaps, 'coverage': coverage, 'owners': owners, 'anchors': anchors}


def find_symbol(text, symbol) -> int | None:
    """1-based line where ``symbol`` is defined (fn/function/class/def/struct/…,
    assignment, or a method definition), or None."""
    if text is None or not symbol:
        return None
    sym = re.escape(symbol)
    defn = re.compile(r'\b(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|function\*?|class|def|struct|enum|trait|interface|type)\s+' + sym + r'\b')
    assign = re.compile(r'(?:^|[\s(,;])(?:const|let|var|export\s+const|export\s+let|window\.)?\s*' + sym + r'\s*=[^=]')
    method = re.compile(r'^\s*(?:export\s+)?(?:async\s+)?(?:static\s+)?' + sym + r'\s*\([^)]*\)\s*(?:->\s*[\w<>:&, ]+\s*)?\{')
    for i, line in enumerate(text.split('\n')):
        if defn.search(line) or assign.search(line) or method.search(line):
            return i + 1
    return None


def find_cycles(comps) -> list[list[str]]:
    ids = {c['id'] for c in comps}
    graph = {c['id']: [d for d in (c.get('depends_on') or []) if d in ids] for c in comps}
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[list[str]] = []
    seen: set[str] = set()

    def dfs(n: str):
        state[n] = 1
        stack.append(n)
        for mm in graph.get(n, []):
            if state.get(mm) == 1:
                cyc = stack[stack.index(mm):] + [mm]
                key = '|'.join(sorted(cyc))
                if key not in seen:
                    seen.add(key)
                    cycles.append(cyc)
            elif not state.get(mm):
                dfs(mm)
        stack.pop()
        state[n] = 2

    for n in graph:
        if not state.get(n):
            dfs(n)
    return cycles


# ── extractor (extract.js) ────────────────────────────────────────────────────

JS_EXTS = {'js', 'mjs', 'cjs', 'jsx', 'ts', 'tsx', 'mts', 'cts'}
JS_RESOLVE = ['', '.js', '.mjs', '.ts', '.tsx', '.jsx', '.cjs', '.json', '/index.js', '/index.ts', '/index.mjs']
TEST_RE = re.compile(r'(^|/)(tests?|__tests__|spec)(/|$)|\.(test|spec)\.[a-z]+$|_test\.[a-z]+$|Tests\.swift$')

_JS_RE = [
    re.compile(r'\bimport\s+(?:[^\'"()]*?\s+from\s+)?[\'"]([^\'"]+)[\'"]'),
    re.compile(r'\brequire\(\s*[\'"]([^\'"]+)[\'"]\s*\)'),
    re.compile(r'\bimport\(\s*[\'"]([^\'"]+)[\'"]\s*\)'),
    re.compile(r'\bexport\s+(?:\*|\{[^}]*\})\s+from\s+[\'"]([^\'"]+)[\'"]'),
]
_RS_MOD_RE = re.compile(r'^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;')
_RS_CRATE_RE = re.compile(r'\bcrate::([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*)')
_PY_IMPORT_RE = re.compile(r'^\s*import\s+([A-Za-z_][\w.]*)')
_PY_FROM_RE = re.compile(r'^\s*from\s+([A-Za-z_][\w.]*)\s+import\b')
_SWIFT_RE = re.compile(r'^\s*(?:@testable\s+)?import\s+([A-Za-z_][A-Za-z0-9_]*)')


def _ext(f: str) -> str:
    b = f[f.rfind('/') + 1:]
    return b[b.rfind('.') + 1:].lower() if '.' in b else ''


def lang_of(f: str) -> str | None:
    e = _ext(f)
    if e in JS_EXTS:
        return 'js'
    if e == 'rs':
        return 'rust'
    if e == 'py':
        return 'python'
    if e == 'swift':
        return 'swift'
    return None


def _dirname(f: str) -> str:
    d = posixpath.dirname(f)
    return '.' if d == '' else d


def _join(*parts: str) -> str:
    # posix.join in node normalizes; posixpath.join does not.
    return posixpath.normpath(posixpath.join(*parts))


def import_specs(lang: str, line: str) -> list[str]:
    out: list[str] = []
    if lang == 'js':
        for rx in _JS_RE:
            out.extend(m.group(1) for m in rx.finditer(line))
    elif lang == 'rust':
        m = _RS_MOD_RE.match(line)
        if m:
            out.append(f'mod:{m.group(1)}')
        out.extend(f'crate:{m.group(1)}' for m in _RS_CRATE_RE.finditer(line))
    elif lang == 'python':
        m = _PY_IMPORT_RE.match(line)
        if m:
            out.append(m.group(1))
        m = _PY_FROM_RE.match(line)
        if m:
            out.append(m.group(1))
    elif lang == 'swift':
        m = _SWIFT_RE.match(line)
        if m:
            out.append(m.group(1))
    return out


def _crate_src_dir(f: str) -> str:
    i = f.rfind('/src/')
    return f[:i + 4] if i >= 0 else _dirname(f)


def resolve_import(lang: str, from_file: str, spec: str, resolve_file) -> str | None:
    d = _dirname(from_file)
    if lang == 'js':
        if not spec.startswith('.') and not spec.startswith('/'):
            return None
        base = _join(d, spec)
        return resolve_file([base + s for s in JS_RESOLVE])
    if lang == 'rust':
        if spec.startswith('mod:'):
            name = spec[4:]
            self_name = posixpath.basename(from_file)
            if self_name.endswith('.rs'):
                self_name = self_name[:-3]
            here = d if self_name in ('mod', 'lib', 'main') else _join(d, self_name)
            return resolve_file([_join(here, f'{name}.rs'), _join(here, name, 'mod.rs'), _join(d, f'{name}.rs'), _join(d, name, 'mod.rs')])
        if spec.startswith('crate:'):
            segs = [s for s in spec[6:].split('::') if s]
            src = _crate_src_dir(from_file)
            for n in range(len(segs), 0, -1):
                p = '/'.join(segs[:n])
                hit = resolve_file([_join(src, f'{p}.rs'), _join(src, p, 'mod.rs')])
                if hit:
                    return hit
            root_file = resolve_file([_join(src, 'lib.rs'), _join(src, 'main.rs')])
            return root_file if root_file and root_file != from_file else None
        return None
    if lang == 'python':
        parts = spec.split('.')
        bases = [d, '', 'src']
        dd = d
        while dd and dd != '.':
            dd = _dirname(dd)
            if dd and dd != '.':
                bases.append(dd)
        for b in bases:
            for n in range(len(parts), 0, -1):
                p = '/'.join(parts[:n])
                hit = resolve_file([_join(b, f'{p}.py'), _join(b, p, '__init__.py')])
                if hit:
                    return hit
        return None
    if lang == 'swift':
        return resolve_file([f'ios/{spec}/App.swift', f'ios/{spec}/Package.swift', f'{spec}/Package.swift'])
    return None


def external_for(spec: str, pkg_to_external: dict, externals: list, lang: str) -> str | None:
    name = spec
    if lang == 'js':
        if spec.startswith('.') or spec.startswith('/'):
            return None
        name = '/'.join(spec.split('/')[:2]) if spec.startswith('@') else spec.split('/')[0]
    elif lang == 'rust':
        return None
    elif lang == 'python':
        name = spec.split('.')[0]
    if name in pkg_to_external:
        return pkg_to_external[name]
    for e in externals:
        if e['id'] == name or e['id'] == f'ext.{name}' or str(e.get('name', '')).lower() == name.lower():
            return e['id']
    return None


def git_head(root) -> str | None:
    try:
        out = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=str(root), capture_output=True, text=True, timeout=20)
        return (out.stdout.strip() or None) if out.returncode == 0 else None
    except Exception:
        return None


def last_change(root, files: list[str]) -> dict | None:
    try:
        out = subprocess.run(['git', 'log', '-1', '--format=%H|%cI|%an', '--', *files], cwd=str(root), capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return None
        s = out.stdout.strip()
        if not s:
            return None
        sha, at, by = s.split('|', 2)
        return {'sha': sha, 'at': at, 'by': by}
    except Exception:
        return None


def iso_now(now=None) -> str:
    dt = now() if callable(now) else (now or datetime.now(timezone.utc))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


def extract_facts(m, root=None, files=None, file_text=None, git=True, now=None, ignore=None) -> dict:
    """Derived facts from the code — same contract as extract.js."""
    root = str(root or os.getcwd())
    v = validate_map(m, root=root, files=files, file_text=file_text, ignore=ignore)
    ft = file_text or (lambda rel: read_text(root, rel))
    file_list = list(files) if files is not None else sorted(list(v['owners'].keys()) + [g['file'] for g in v['gaps']])
    file_set = set(file_list)
    owners = v['owners']
    comp_by_id = {c['id']: c for c in m['components']} if isinstance(m.get('components'), list) else {}
    externals = m.get('externals') or [] if isinstance(m, dict) else []
    pkg_to_external: dict[str, str] = {}
    for e in externals:
        for p in e.get('packages') or []:
            pkg_to_external[p] = e['id']
    use_git = git is not False

    components: dict[str, dict] = {}
    by_owner: dict[str, list[str]] = {}
    for f, c in owners.items():
        by_owner.setdefault(c, []).append(f)
    for c in comp_by_id.values():
        lst = sorted(by_owner.get(c['id'], []))
        loc = 0
        tests = 0
        langs: set[str] = set()
        for f in lst:
            loc += count_lines(ft(f))
            if TEST_RE.search(f):
                tests += 1
            lg = lang_of(f)
            if lg:
                langs.add(lg)
        components[c['id']] = {'files': len(lst), 'loc': loc, 'tests': tests, 'languages': sorted(langs),
                               'last_change': last_change(root, lst) if (use_git and lst) else None}

    edge_map: dict[str, dict] = {}

    def add_edge(frm, to, kind, file, line):
        if not frm or not to or frm == to:
            return
        key = f'{frm}→{to}'
        e = edge_map.get(key) or {'from': frm, 'to': to, 'kind': kind, 'evidence': []}
        if len(e['evidence']) < 50:
            e['evidence'].append({'file': file, 'line': line})
        edge_map[key] = e

    def resolve_file(candidates):
        for cand in candidates:
            if cand in file_set:
                return cand
        return None

    for f in file_list:
        frm = owners.get(f)
        if not frm:
            continue
        text = ft(f)
        if text is None:
            continue
        lg = lang_of(f)
        if not lg:
            continue
        for i, line in enumerate(text.split('\n')):
            for spec in import_specs(lg, line):
                target = resolve_import(lg, f, spec, resolve_file)
                if target:
                    to = owners.get(target)
                    if to:
                        add_edge(frm, to, 'imports', f, i + 1)
                    elif target not in owners:
                        add_edge(frm, f'gap:{target}', 'imports', f, i + 1)
                else:
                    ext_id = external_for(spec, pkg_to_external, externals, lg)
                    if ext_id:
                        add_edge(frm, ext_id, 'imports', f, i + 1)

    relations = sorted((e for e in edge_map.values() if not e['to'].startswith('gap:')), key=lambda e: e['from'] + e['to'])
    gap_edges = [e for e in edge_map.values() if e['to'].startswith('gap:')]

    conflicts: list[dict] = []
    declared = {c['id']: set(c.get('depends_on') or []) for c in comp_by_id.values()}
    derived: dict[str, set[str]] = {}
    for r in relations:
        derived.setdefault(r['from'], set()).add(r['to'])
    for r in relations:
        if r['to'] not in declared.get(r['from'], set()):
            conflicts.append({'kind': 'undeclared_dependency', 'from': r['from'], 'to': r['to'], 'evidence': r['evidence'][:3]})
    for c in comp_by_id.values():
        for d in c.get('depends_on') or []:
            if d not in comp_by_id:
                continue
            if d in derived.get(c['id'], set()):
                continue
            a = components.get(c['id'])
            b = components.get(d)
            if not a or not a['files'] or not b or not b['files']:
                continue
            if not any(x in b['languages'] for x in a['languages']):
                continue
            conflicts.append({'kind': 'dead_dependency', 'from': c['id'], 'to': d, 'evidence': []})

    return {
        'extracted_at': iso_now(now),
        'source_revision': git_head(root) if use_git else None,
        'extractor': dict(EXTRACTOR),
        'components': components,
        'relations': relations,
        'gaps': [{'file': g['file'], 'reason': g['reason'], 'imported_by': [e['from'] for e in gap_edges if e['to'] == f'gap:{g["file"]}']} for g in v['gaps']],
        'conflicts': conflicts,
        'validation': {'ok': v['ok'], 'errors': len(v['errors']), 'warnings': len(v['warnings']), 'coverage': v['coverage']},
    }


# ── loading, reporting, querying (index.js + query.py) ────────────────────────

def load_map(root, file: str = DEFAULT_MAP_FILE) -> dict:
    path = Path(file) if str(file).startswith('/') else Path(root) / file
    with open(path, 'r', encoding='utf-8') as fh:
        return json.load(fh)


def report_table(m, derived) -> list[dict]:
    declared_out = {c['id']: len(c.get('depends_on') or []) for c in m['components']}
    derived_out: dict[str, int] = {}
    for r in derived['relations']:
        derived_out[r['from']] = derived_out.get(r['from'], 0) + 1
    conflicts: dict[str, int] = {}
    for c in derived['conflicts']:
        conflicts[c['from']] = conflicts.get(c['from'], 0) + 1
    rows = []
    for c in m['components']:
        f = derived['components'].get(c['id']) or {'files': 0, 'loc': 0, 'tests': 0, 'last_change': None}
        rows.append({
            'component': c['id'], 'subsystem': c['subsystem'], 'kind': c['kind'], 'files': f['files'], 'loc': f['loc'], 'tests': f['tests'],
            'deps_declared': declared_out.get(c['id'], 0), 'deps_derived': derived_out.get(c['id'], 0), 'conflicts': conflicts.get(c['id'], 0),
            'last_change': f['last_change']['at'][:10] if f.get('last_change') else '',
        })
    return rows


def format_validation(v: dict) -> str:
    lines = []
    for e in v['errors']:
        lines.append(f'ERROR   {e["code"]}: {e["message"]}')
    for w in v['warnings']:
        lines.append(f'warning {w["code"]}: {w["message"]}')
    cov = v['coverage']
    g = len(v['gaps'])
    lines.append(f'coverage: {cov["mapped"]}/{cov["files"]} source files owned ({cov["percent"]}%), {g} gap{"" if g == 1 else "s"}, {len(v["anchors"])} anchors resolved')
    if g:
        lines.append('gaps (unmapped files):')
        for gap in v['gaps'][:40]:
            lines.append(f'  - {gap["file"]}')
        if g > 40:
            lines.append(f'  … +{g - 40}')
    n = len(v['errors'])
    lines.append('OK' if v['ok'] else f'FAILED with {n} error{"" if n == 1 else "s"}')
    return '\n'.join(lines)


def format_table(rows: list[dict], derived: dict | None = None) -> str:
    cols = ['component', 'kind', 'files', 'loc', 'tests', 'deps_declared', 'deps_derived', 'conflicts', 'last_change']
    width = {c: max(len(c), *(len(str(r[c])) for r in rows)) if rows else len(c) for c in cols}
    line = lambda r: '  '.join(str(r[c]).ljust(width[c]) for c in cols)  # noqa: E731
    out = [line({c: c for c in cols}), '  '.join('-' * width[c] for c in cols)]
    out.extend(line(r) for r in rows)
    if derived is not None:
        rev = derived.get('source_revision')
        out.append('')
        out.append(f'relations: {len(derived["relations"])}   gaps: {len(derived["gaps"])}   conflicts: {len(derived["conflicts"])}   revision: {rev[:8] if rev else "-"}')
        for c in derived['conflicts']:
            ev = c.get('evidence') or []
            out.append(f'  {c["kind"]}: {c["from"]} → {c["to"]}' + (f' ({ev[0]["file"]}:{ev[0]["line"]})' if ev else ''))
    return '\n'.join(out)


def query_component(m, derived, component_id: str) -> dict | None:
    """Merged declared + derived view for one component (the skill's `query`)."""
    comp = next((c for c in m['components'] if c['id'] == component_id), None)
    if comp is None:
        return None
    facts = derived['components'].get(component_id) or {'files': 0, 'loc': 0, 'tests': 0, 'languages': [], 'last_change': None}
    outgoing = [r for r in derived['relations'] if r['from'] == component_id]
    incoming = [r for r in derived['relations'] if r['to'] == component_id]
    declared = list(comp.get('depends_on') or [])
    derived_targets = [r['to'] for r in outgoing]
    return {
        'id': comp['id'], 'name': comp['name'], 'subsystem': comp['subsystem'], 'kind': comp['kind'],
        'description': comp.get('description', ''), 'code': list(comp.get('code') or []),
        'entrypoints': list(comp.get('entrypoints') or []), 'interfaces': list(comp.get('interfaces') or []),
        'invariants': list(comp.get('invariants') or []), 'docs': list(comp.get('docs') or []), 'owner_role': comp.get('owner_role'),
        'depends_on': {'declared': declared, 'derived': derived_targets,
                       'undeclared': [t for t in derived_targets if t not in declared],
                       'unobserved': [d for d in declared if d not in derived_targets]},
        'depended_on_by': sorted({r['from'] for r in incoming}),
        'facts': facts,
        'conflicts': [c for c in derived['conflicts'] if c['from'] == component_id or c['to'] == component_id],
        'freshness': {'extracted_at': derived.get('extracted_at'), 'source_revision': derived.get('source_revision')},
        'provenance': {'declared': 'system-map.json', 'derived': f'{derived["extractor"]["name"]}@{derived["extractor"]["version"]}'},
    }
