"""Bundle export/import/validation, cross-language interchange with Acheron's kernel, CLI."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from charon.workspace import WorkspaceStore, import_bundle, read_bundle, records_equal, validate_bundle
from charon.workspace.bundle import SCHEMA_PATH, load_schema, write_bundle_files

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'examples' / 'workspaces' / 'acheron-overseer.json'
CHARON_EXAMPLE = ROOT / 'examples' / 'workspaces' / 'charon-workspace.json'
OV = {'kind': 'agent', 'id': 'agent.overseer.t'}


def test_schema_and_fixture_exist():
    assert SCHEMA_PATH.exists() and load_schema()['$schema'].startswith('https://json-schema.org/')
    assert FIXTURE.exists()


def test_acheron_fixture_imports_verifies_and_round_trips(tmp_path):
    """The bundle was produced by Acheron's JS kernel; Python must reproduce its 31 digests."""
    bundle = read_bundle(FIXTURE)
    assert validate_bundle(bundle) == []
    ws = import_bundle(tmp_path / 'ws', bundle)
    assert ws.replica_id == 'replica.acheron.fixture' and ws.workspace_id == 'workspace.acheron.e2e-workspace-0001'
    assert ws.verify_chain() == {'ok': True, 'count': 31, 'broken_at': None, 'reason': None}
    assert ws.seq == 31 and len(ws.events) == 31
    assert ws.get('work_item', 'work.run-test-sh.fixture')['status'] == 'done'
    assert ws.get('task', 'task.fixture1')['status'] == 'succeeded'
    assert ws.list('gate')[0]['question'] == 'Drop mosh from scope?'
    exported = ws.export_bundle()
    assert validate_bundle(exported) == []
    assert records_equal(bundle, exported) == []
    again = import_bundle(tmp_path / 'ws2', exported)
    assert records_equal(exported, again.export_bundle()) == []
    assert again.verify_chain()['count'] == 31
    # continuing the Acheron chain from Python keeps it valid
    again.append_event('cycle.started', payload={'cycle': 4})
    assert again.verify_chain() == {'ok': True, 'count': 32, 'broken_at': None, 'reason': None}
    assert again.events[-1]['previous_digest'] == bundle['events'][-1]['digest']


def test_charon_example_bundle_still_validates():
    if not CHARON_EXAMPLE.exists():
        pytest.skip('charon example bundle absent')
    assert validate_bundle(read_bundle(CHARON_EXAMPLE)) == []


def test_export_of_fresh_store_validates_and_reimports(tmp_path):
    ws = WorkspaceStore.open(tmp_path / 'a', workspace_id='workspace.test.fresh', replica_id='replica.test.a', slug='fresh', title='Fresh',
                             roots=[{'kind': 'directory', 'locator': '/proj'}])
    wi = ws.create('work_item', {'kind': 'task', 'title': 'T', 'acceptance_criteria': [{'statement': 's', 'verifier': {'kind': 'approval'}}]}, actor=OV)
    run = ws.create('run', {'work_item_ids': [wi['id']]}, actor=OV)
    task = ws.create('task', {'work_item_id': wi['id'], 'run_id': run['id'], 'title': 't', 'instruction': 'i', 'session_id': 'session.b'}, actor=OV)
    ws.create('session', {'id': 'session.b', 'kind': 'worker', 'callsign': 'Builder'}, actor=OV)
    ws.lease_acquire(task_id=task['id'], session_id='session.b', scopes=[{'selector': 'src'}])
    ws.put_artifact(text='x', produced_by=OV)
    ws.create('policy', {'name': 'write_requires_wire', 'actions': ['dispatch'], 'effect': 'deny'}, actor=OV)
    ws.create_gate(question='ok?', actor=OV)
    ws.create_proposal(kind='scope', title='cut', rationale='time', actor=OV)
    bundle = ws.export_bundle()
    assert validate_bundle(bundle) == []
    assert {k: len(v) for k, v in bundle.items() if isinstance(v, list) and v} == {
        'work_items': 1, 'runs': 1, 'tasks': 1, 'leases': 1, 'sessions': 1, 'artifacts': 1, 'policies': 1, 'replicas': 1, 'events': len(ws.events)}
    ws2 = import_bundle(tmp_path / 'b', bundle)
    assert records_equal(bundle, ws2.export_bundle()) == [] and ws2.verify_chain()['ok']
    assert ws2.list('proposal')[0]['title'] == 'cut'


def test_validate_reports_schema_errors():
    bundle = read_bundle(FIXTURE)
    bundle['work_items'][0]['status'] = 'bogus'
    errors = validate_bundle(bundle)
    assert errors and any('work_items[0]' in e for e in errors)


def test_write_bundle_files_layout(tmp_path):
    bundle = read_bundle(FIXTURE)
    root = write_bundle_files(tmp_path / 'layout', bundle)
    assert (root / 'workspace.json').exists()
    assert set(p.name for p in (root / 'records').glob('*.json')) >= {'work_item.json', 'task.json', 'lease.json', 'gate.json', 'proposal.json'}
    assert (root / 'events' / 'replica.acheron.fixture.jsonl').exists()
    assert (root / 'manifests' / 'manifest.fixture1.json').exists()


def test_cli_import_validate_status_verify_export(tmp_path):
    root = tmp_path / 'cli'
    env = {'PYTHONPATH': str(ROOT / 'src')}
    def run(*args):
        return subprocess.run([sys.executable, '-m', 'charon.workspace', *args], capture_output=True, text=True, env=env, cwd=ROOT)
    r = run('import', '--root', str(root), '--bundle', str(FIXTURE))
    assert r.returncode == 0, r.stderr
    assert '31 events' in r.stdout and "'ok': True" in r.stdout
    r = run('validate', '--root', str(root))
    assert r.returncode == 0 and r.stdout.strip() == 'valid'
    r = run('status', '--root', str(root))
    assert r.returncode == 0 and 'work items: 2 total, 1 done' in r.stdout
    r = run('verify', '--root', str(root))
    assert r.returncode == 0 and json.loads(r.stdout)['count'] == 31
    out = tmp_path / 'out.json'
    r = run('export', '--root', str(root), '--bundle', str(out))
    assert r.returncode == 0 and validate_bundle(json.loads(out.read_text())) == []
    bad = tmp_path / 'bad.json'
    b = read_bundle(FIXTURE)
    b['tasks'][0]['status'] = 'nope'
    bad.write_text(json.dumps(b))
    r = run('validate', '--bundle', str(bad))
    assert r.returncode == 2 and 'error(s)' in r.stdout
