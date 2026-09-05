"""System map skill (skills/system-map): parity with Acheron's JS map core on the
shared fixture repo, the SystemMap tool, the skill record, and the SKILL.md installer."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from charon.tools import ToolContext, ALL_TOOL_DEFS, TOOL_EXECUTORS
from charon.tools import system_map_tool
from charon.workspace import system_map_skill as sms
from charon.workspace.system_map_skill import (install_system_map_skill, load_skill_module, skill_path, skill_record, skills_dir)

REPO = Path(__file__).resolve().parents[1]
ACHERON = Path(os.environ.get('ACHERON_WORKTREE', str(Path.home() / 'Projects' / 'acheron-overseer')))
FX = ACHERON / 'tests' / 'fixtures' / 'system-map'
FX_REPO = FX / 'repo'
FIXTURE_MAPS = ['map-ok', 'map-overlap', 'map-undeclared', 'map-dead', 'map-anchor', 'map-cycle', 'map-badshape']
NODE = shutil.which('node')

needs_fixtures = pytest.mark.skipif(not FX_REPO.is_dir(), reason=f'Acheron fixture repo not found at {FX_REPO}')
needs_node = pytest.mark.skipif(NODE is None, reason='node is not installed')

core = load_skill_module('mapcore')


def _fixture_map(name: str) -> dict:
    return json.loads((FX / f'{name}.json').read_text())


def _js(module_expr: str) -> dict:
    """Run a snippet against Acheron's JS map core and return its JSON."""
    script = (
        "import { validateMap } from './src/overseer/map/validate.js';"
        "import { extractFacts } from './src/overseer/map/extract.js';"
        "import { readFileSync } from 'node:fs';"
        f"const out = ({module_expr});"
        "console.log(JSON.stringify(out));"
    )
    r = subprocess.run([NODE, '--no-warnings', '--input-type=module', '-e', script], cwd=str(ACHERON), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _norm(v):
    """Order-insensitive, time-insensitive view for cross-language comparison."""
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items() if k not in ('extracted_at', 'source_revision', 'message')}
    if isinstance(v, list):
        items = [_norm(x) for x in v]
        return sorted(items, key=lambda x: json.dumps(x, sort_keys=True))
    return v


# ── parity with the JS core ───────────────────────────────────────────────────

@needs_fixtures
@needs_node
@pytest.mark.parametrize('name', FIXTURE_MAPS)
def test_validate_matches_js_on_fixture(name):
    m = _fixture_map(name)
    py = core.validate_map(m, root=str(FX_REPO))
    js = _js(f"(() => {{ const v = validateMap(JSON.parse(readFileSync('tests/fixtures/system-map/{name}.json','utf8')), {{ root: 'tests/fixtures/system-map/repo' }});"
             " return { ok: v.ok, errors: v.errors, warnings: v.warnings, gaps: v.gaps, coverage: v.coverage, owners: v.owners, anchors: v.anchors }; })()")
    assert py['ok'] == js['ok']
    assert _norm(py['errors']) == _norm(js['errors'])
    assert _norm(py['warnings']) == _norm(js['warnings'])
    assert _norm(py['gaps']) == _norm(js['gaps'])
    assert py['coverage'] == js['coverage']
    assert py['owners'] == js['owners']
    assert _norm(py['anchors']) == _norm(js['anchors'])
    # messages too (the one field _norm drops), in order
    assert [e['message'] for e in py['errors']] == [e['message'] for e in js['errors']]
    assert [w['message'] for w in py['warnings']] == [w['message'] for w in js['warnings']]


@needs_fixtures
@needs_node
@pytest.mark.parametrize('name', ['map-ok', 'map-undeclared', 'map-dead', 'map-cycle'])
def test_extract_matches_js_on_fixture(name):
    m = _fixture_map(name)
    py = core.extract_facts(m, root=str(FX_REPO), git=False)
    js = _js(f"extractFacts(JSON.parse(readFileSync('tests/fixtures/system-map/{name}.json','utf8')), {{ root: 'tests/fixtures/system-map/repo', git: false }})")
    assert py['extractor'] == js['extractor'] == {'name': 'system-map', 'version': '1'}
    assert py['components'] == js['components']
    assert _norm(py['relations']) == _norm(js['relations'])
    assert _norm(py['gaps']) == _norm(js['gaps'])
    assert _norm(py['conflicts']) == _norm(js['conflicts'])
    assert py['validation'] == js['validation']
    assert py['source_revision'] is None and js['source_revision'] is None


@needs_fixtures
def test_fixture_expectations_hold_in_python():
    """The rules the plan promises, checked directly (independent of node)."""
    ok = core.validate_map(_fixture_map('map-ok'), root=str(FX_REPO))
    assert ok['ok'] and [g['file'] for g in ok['gaps']] == ['orphan/stray.txt']
    assert ok['owners']['rs/src/store.rs'] == 'core.store' and ok['coverage'] == {'files': 12, 'mapped': 11, 'percent': 92}
    assert any(a['component'] == 'core.store' and a['symbol'] == 'Db' and a['line'] == 1 for a in ok['anchors'])
    over = core.validate_map(_fixture_map('map-overlap'), root=str(FX_REPO))
    assert not over['ok'] and over['errors'][0]['code'] == 'overlap' and over['errors'][0]['components'] == ['web.app', 'core.lib']
    anch = core.validate_map(_fixture_map('map-anchor'), root=str(FX_REPO))
    assert [e['code'] for e in anch['errors']] == ['anchor_unresolved'] and anch['owners']['lib/util.js'] == 'core.lib'
    cyc = core.validate_map(_fixture_map('map-cycle'), root=str(FX_REPO))
    assert cyc['ok'] and cyc['warnings'][0]['cycle'] == ['web.app', 'core.lib', 'web.app']
    bad = core.validate_map(_fixture_map('map-badshape'), root=str(FX_REPO))
    assert not bad['ok'] and bad['errors'][0]['code'] == 'schema' and 'must be a slug' in bad['errors'][0]['message']
    dead = core.extract_facts(_fixture_map('map-dead'), root=str(FX_REPO), git=False)
    kinds = {(c['kind'], c['from'], c['to']) for c in dead['conflicts']}
    assert ('dead_dependency', 'web.b', 'web.app') in kinds and ('undeclared_dependency', 'web.app', 'web.b') in kinds
    okd = core.extract_facts(_fixture_map('map-ok'), root=str(FX_REPO), git=False)
    rel = {(r['from'], r['to']): r['evidence'] for r in okd['relations']}
    assert rel[('core.rs', 'core.store')] == [{'file': 'rs/src/lib.rs', 'line': 2}, {'file': 'rs/src/net/mod.rs', 'line': 1}]
    assert rel[('web.app', 'ext.xterm')] == [{'file': 'src/a.js', 'line': 3}] and okd['conflicts'] == []


@needs_fixtures
def test_walk_and_glob_semantics():
    files = core.walk_source_files(str(FX_REPO))
    assert 'src/a.js' in files and 'rs/src/net/mod.rs' in files and 'py/app/core.py' in files
    assert not any(f.startswith('ignored-dir/') for f in files) and 'debug.log' not in files
    assert files == sorted(files)
    assert core.match_glob('src/**', 'src/deep/er/a.js') and core.match_glob('src/**/*.js', 'src/a.js')
    assert not core.match_glob('src/*.js', 'src/x/a.js') and core.match_glob('src/*.{js,ts}', 'src/a.ts')
    assert core.match_glob('a/b?.js', 'a/b1.js') and not core.match_glob('a/b?.js', 'a/b/1.js')
    assert core.parse_code_ref('src/main.js#initTerminal') == {'path': 'src/main.js', 'symbol': 'initTerminal', 'anchored': True}
    assert core.find_symbol('fn a() {}\npub async fn run_loop(x: i32) {}', 'run_loop') == 2
    assert core.find_symbol('export function boot() {}', 'boot') == 1
    assert core.find_symbol('const x = 1;\nwindow._go = () => 1;', '_go') == 2
    assert core.find_symbol('  async handle(req) {\n', 'handle') == 1
    assert core.find_symbol('nothing here', 'boot') is None
    assert len(core.parse_gitignore('# c\nnode_modules/\n*.p8\n!keep.p8\n/abs/only\n')) == 3


@pytest.mark.skipif(not (ACHERON / 'system-map.json').is_file(), reason='Acheron worktree with system-map.json not found')
def test_acherons_own_map_validates_and_extracts():
    m = core.load_map(str(ACHERON))
    v = core.validate_map(m, root=str(ACHERON))
    assert v['ok'], [e['message'] for e in v['errors']]
    assert v['coverage']['percent'] >= 90
    d = core.extract_facts(m, root=str(ACHERON), git=False)
    assert len(d['relations']) >= 1 and all(r['evidence'] for r in d['relations'])


@needs_fixtures
def test_cli_scripts_exit_codes_and_output(tmp_path):
    py = sys.executable
    sk = skills_dir()
    ok = subprocess.run([py, str(sk / 'validate.py'), '--root', str(FX_REPO), '--map', '../map-ok.json'], capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.strip().endswith('OK') and 'coverage: 11/12' in ok.stdout
    bad = subprocess.run([py, str(sk / 'validate.py'), '--root', str(FX_REPO), '--map', '../map-overlap.json'], capture_output=True, text=True)
    assert bad.returncode == 1 and 'ERROR   overlap' in bad.stdout
    js = subprocess.run([py, str(sk / 'validate.py'), '--root', str(FX_REPO), '--map', '../map-ok.json', '--json'], capture_output=True, text=True)
    assert json.loads(js.stdout)['ok'] is True
    out = tmp_path / 'derived.json'
    ex = subprocess.run([py, str(sk / 'extract.py'), '--root', str(FX_REPO), '--map', '../map-ok.json', '--out', str(out), '--no-git'], capture_output=True, text=True)
    assert ex.returncode == 0 and out.is_file() and 'wrote' in ex.stdout
    d = json.loads(out.read_text())
    assert set(d) == {'extracted_at', 'source_revision', 'extractor', 'components', 'relations', 'gaps', 'conflicts', 'validation'}
    q = subprocess.run([py, str(sk / 'query.py'), '--root', str(FX_REPO), '--map', '../map-ok.json', '--derived', str(out), 'core.rs'], capture_output=True, text=True)
    assert q.returncode == 0 and 'core.rs — core.rs' in q.stdout and 'depends_on (derived):  core.store' in q.stdout
    qt = subprocess.run([py, str(sk / 'query.py'), '--root', str(FX_REPO), '--map', '../map-ok.json', '--derived', str(out)], capture_output=True, text=True)
    assert qt.returncode == 0 and 'relations: 3' in qt.stdout
    miss = subprocess.run([py, str(sk / 'query.py'), '--root', str(FX_REPO), '--map', '../map-ok.json', '--derived', str(out), 'nope'], capture_output=True, text=True)
    assert miss.returncode == 1 and 'unknown component' in miss.stderr


# ── the SystemMap tool ────────────────────────────────────────────────────────

def _ctx(tmp_path, project: Path | None = None) -> ToolContext:
    return ToolContext(project_root=project or tmp_path, agent_id='AG-1', state_dir=tmp_path / 'state')


def test_tool_is_registered_with_a_valid_schema():
    d = next(t for t in ALL_TOOL_DEFS if t['name'] == 'SystemMap')
    assert d['input_schema']['required'] == ['action'] and 'properties' in d['input_schema']
    assert TOOL_EXECUTORS['SystemMap'] is system_map_tool.execute_system_map


@needs_fixtures
def test_tool_actions(tmp_path):
    # Work on a copy of the fixture repo so `map` can be the default file name.
    root = tmp_path / 'proj'
    shutil.copytree(FX_REPO, root)
    (root / 'system-map.json').write_text((FX / 'map-ok.json').read_text())
    ctx = _ctx(tmp_path, root)
    v = system_map_tool.execute_system_map({'action': 'validate'}, ctx)
    assert not v.is_error and v.details['ok'] is True and v.details['coverage']['mapped'] == 11 and 'OK' in v.content
    e = system_map_tool.execute_system_map({'action': 'extract', 'no_git': True, 'out': 'derived.json'}, ctx)
    assert not e.is_error and len(e.details['relations']) == 3 and (root / 'derived.json').is_file()
    q = system_map_tool.execute_system_map({'action': 'query', 'component': 'web.app', 'derived': 'derived.json'}, ctx)
    assert not q.is_error and q.details['depends_on'] == {'declared': ['core.lib', 'ext.xterm'], 'derived': ['core.lib', 'ext.xterm'], 'undeclared': [], 'unobserved': []}
    t = system_map_tool.execute_system_map({'action': 'query', 'no_git': True}, ctx)
    # 3 gaps in the copy: orphan/stray.txt plus the system-map.json and derived.json we wrote (unowned by the fixture map)
    assert not t.is_error and len(t.details['rows']) == 6 and t.details['gaps'] == 3
    bad = system_map_tool.execute_system_map({'action': 'query', 'component': 'nope', 'derived': 'derived.json'}, ctx)
    assert bad.is_error and 'unknown component' in bad.content
    nomap = system_map_tool.execute_system_map({'action': 'validate', 'root': str(tmp_path)}, ctx)
    assert nomap.is_error and 'declare one first' in nomap.content
    assert system_map_tool.execute_system_map({'action': 'dance'}, ctx).is_error


# ── the skill record + SKILL.md installer ─────────────────────────────────────

def test_skill_record_validates_against_the_workspace_contract():
    jsonschema = pytest.importorskip('jsonschema')
    schema = json.loads((REPO / 'docs' / 'contracts' / 'workspace.schema.json').read_text())
    rec = skill_record(workspace_id='workspace.charon.test')
    sub = {'$schema': schema.get('$schema', 'https://json-schema.org/draft/2020-12/schema'), '$defs': schema['$defs'], '$ref': '#/$defs/skill'}
    errs = list(jsonschema.Draft202012Validator(sub, format_checker=jsonschema.FormatChecker()).iter_errors(rec))
    assert errs == [], [e.message for e in errs]
    assert rec['name'] == 'system-map' and rec['capabilities'] == ['map.extract', 'map.validate', 'map.query']
    assert rec['invocation']['kind'] == 'command' and rec['invocation']['entrypoint'].endswith('skills/system-map/extract.py')


def test_skill_md_installs_and_is_listed_by_the_skills_tool(tmp_path):
    from charon.tools import skills_tool
    state = tmp_path / 'state'
    p = install_system_map_skill(state)
    assert p == skill_path(state) and 'Declared beats inferred' in p.read_text() and 'query' in p.read_text()
    p.write_text('# edited')
    install_system_map_skill(state)
    assert p.read_text() == '# edited'
    install_system_map_skill(state, overwrite=True)
    assert 'system-map' in p.read_text()
    (tmp_path / 'proj').mkdir()
    r = skills_tool.execute_skills({'action': 'list'}, ToolContext(project_root=tmp_path / 'proj', agent_id='AG-1', state_dir=state))
    assert not r.is_error and 'system-map' in r.content


def test_skill_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv('CHARON_SKILLS_DIR', str(tmp_path / 'skills'))
    assert skills_dir() == tmp_path / 'skills' / 'system-map'
    assert 'Declared beats inferred' in sms.skill_md_text()  # falls back to the embedded text
    with pytest.raises(FileNotFoundError):
        load_skill_module('mapcore-missing')


# ── contract sync with Acheron ────────────────────────────────────────────────

@pytest.mark.skipif(not (ACHERON / 'docs' / 'contracts' / 'system-map.schema.json').is_file(), reason='Acheron worktree not found')
def test_vendored_system_map_schema_is_identical_to_acherons():
    ours = (REPO / 'docs' / 'contracts' / 'system-map.schema.json').read_bytes()
    theirs = (ACHERON / 'docs' / 'contracts' / 'system-map.schema.json').read_bytes()
    assert ours == theirs
