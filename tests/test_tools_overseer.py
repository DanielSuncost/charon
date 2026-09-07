"""Overseer tools over charon.workspace with a fake transport — the Acheron dispatcher
scenario (tests/overseer/dispatcher.test.mjs) ported handler for handler, plus an MCP
smoke through ``python -m charon.mcp_server --profile overseer``."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from charon.tools import ToolContext
from charon.tools import overseer_tool as OT
from charon.workspace.transport import extract_last_message

OVERSEER, BUILDER, TESTER, SHELL = 'session.ov', 'session.b1', 'session.t1', 'session.sh'


class FakeTransport:
    name = 'fake'

    def __init__(self):
        self.sessions = [
            {'id': OVERSEER, 'name': 'ach-ov', 'block_id': 'ov', 'callsign': 'Overseer', 'title': 'Overseer', 'agent': 'claude', 'kind': 'claude', 'status': 'idle', 'host_ref': 'local', 'cwd': '/ov', 'role': 'overseer'},
            {'id': BUILDER, 'name': 'ach-b1', 'block_id': 'b1', 'callsign': 'Builder', 'title': 'Builder', 'agent': 'claude', 'kind': 'claude', 'status': 'idle', 'host_ref': 'local', 'cwd': '/proj'},
            {'id': TESTER, 'name': 'ach-t1', 'block_id': 't1', 'callsign': 'Tester', 'title': 'Tester', 'agent': 'codex', 'kind': 'claude', 'status': 'idle', 'host_ref': 'local', 'cwd': '/proj'},
            {'id': SHELL, 'name': 'ach-sh', 'block_id': 'sh', 'callsign': 'Shell', 'title': 'Shell', 'agent': None, 'kind': 'terminal', 'status': 'idle', 'host_ref': 'local', 'cwd': '/proj'},
        ]
        self.sent: dict[str, list[str]] = {}
        self.screens: dict[str, str] = {}
        self.labels: dict[str, str] = {}

    def by(self, sid):
        return next(s for s in self.sessions if s['id'] == sid)

    def list_sessions(self):
        return [dict(s) for s in self.sessions]

    def read(self, sid, mode='last_message', lines=2000):
        raw = self.screens.get(sid, '')
        return extract_last_message(raw) if mode == 'last_message' else raw

    def send(self, sid, text, *, enter=True):
        self.sent.setdefault(sid, []).append(text + ('\n' if enter else ''))
        return 'fake'

    def interrupt(self, sid, key='ctrl-c'):
        self.sent.setdefault(sid, []).append(f'<{key}>')

    def foreground(self, sid):
        return 'claude'

    def label(self, sid, text):
        self.labels[sid] = text

    def spawn(self, role, agent, cwd, prompt):
        s = {'id': f'session.{role.lower()}', 'name': f'ach-{role.lower()}', 'block_id': role.lower(), 'callsign': role, 'title': role,
             'agent': agent, 'kind': 'claude', 'status': 'idle', 'host_ref': 'local', 'cwd': cwd}
        self.sessions.append(s)
        return s


class Env:
    def __init__(self, tmp_path: Path):
        OT.reset_store_cache()
        self.proj = tmp_path / 'proj'
        self.proj.mkdir()
        self.root = tmp_path / 'ws'
        self.transport = FakeTransport()
        self.policy = {'wired': [BUILDER, TESTER], 'user_typed_ago_ms': {}}
        self.ctx = ToolContext(project_root=self.proj, agent_id='overseer', state_dir=tmp_path / 'state',
                               metadata={'workspace_root': str(self.root), 'transport': self.transport, 'policy': self.policy, 'surface': 'test'})

    def call(self, name, **args):
        r = OT.execute_overseer(name, args, self.ctx)
        if r.is_error:
            raise RuntimeError(r.content)
        assert json.loads(r.content) == json.loads(json.dumps(r.details, default=str))
        return r.details

    def store(self):
        return OT.open_store(self.ctx)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def rejects(fn, needle):
    with pytest.raises(RuntimeError) as ei:
        fn()
    assert needle in str(ei.value), str(ei.value)
    return str(ei.value)


def plan(env):
    obj = env.call('acheron_work_create', kind='objective', title='Ship hub MCP', priority='high')
    wi = env.call('acheron_work_create', kind='task', title='Wire MCP endpoint', parent_id=obj['id'], owner_role='Builder',
                  scopes=[{'selector': 'src-tauri/src/hub.rs', 'access': 'write'}],
                  acceptance_criteria=[{'statement': 'cargo test passes', 'verifier': {'kind': 'command', 'spec': {'command': 'cargo test'}}}])
    wi = env.call('acheron_work_transition', id=wi['id'], expected_revision=wi['revision'], event='ready')
    return obj, wi


def test_all_contract_tools_have_handlers_and_are_registered():
    names = {t['name'] for t in OT.CONTRACT}
    assert names == set(OT.HANDLERS) and len(names) == 20
    from charon import tools
    for t in OT.CONTRACT:
        assert t['charon_name'] in tools.TOOL_EXECUTORS
        assert any(d['name'] == t['charon_name'] for d in tools.ALL_TOOL_DEFS)


def test_fleet_syncs_sessions_with_wired_flags_and_records_them(env):
    r = env.call('acheron_fleet')
    ws = r['workspaces'][0]
    assert ws['own'] and r['overseer_workspace']['transport'] == 'fake'
    by = {s['callsign']: s for s in ws['sessions']}
    assert by['Builder']['wired'] and by['Tester']['wired'] and not by['Shell']['wired']
    assert by['Overseer']['role'] == 'overseer' and not by['Overseer']['wired']
    st = env.store()
    assert st.get('session', BUILDER)['status'] == 'idle' and st.get('session', OVERSEER)['kind'] == 'coordinator'
    assert (env.root / 'journal.jsonl').exists()


def test_read_redacts_and_keeps_an_artifact(env):
    env.transport.screens[BUILDER] = '> fix the hub\nI changed hub.rs, token was sk-ant-abcdefghijklmnopqrstuvwxyz1234\n✓ Done.\n❯ '
    r = env.call('acheron_read', session='builder', keep=True)
    assert r['session'] == BUILDER and 'hub.rs' in r['text'] and 'abcdefghijklmnop' not in r['text']
    assert r['artifact_id'].startswith('artifact.sha256:') and len(r['digest']) == 64
    assert env.store().get('artifact', r['artifact_id'])['session_id'] == BUILDER
    rejects(lambda: env.call('acheron_read', session='nobody'), 'no session matches')
    assert env.call('acheron_read', session='t')['session'] == TESTER  # unique prefix resolves
    env.transport.sessions.append({**env.transport.by(TESTER), 'id': 'session.t2', 'name': 'ach-t2', 'block_id': 't2', 'callsign': 'Testbed', 'title': 'Testbed'})
    rejects(lambda: env.call('acheron_read', session='Test'), 'ambiguous')
    env.transport.sessions.pop()


def test_work_items_create_ready_list_and_cas(env):
    obj, wi = plan(env)
    assert obj['status'] == 'backlog' and wi['status'] == 'ready' and len(wi['acceptance_criteria']) == 1
    listed = env.call('acheron_work_list', owner_role='builder')
    assert listed['count'] == 1 and listed['items'][0]['criteria'][0]['verifier_kind'] == 'command'
    rejects(lambda: env.call('acheron_work_transition', id=wi['id'], expected_revision=1, event='start'), 'revision')
    up = env.call('acheron_work_update', id=wi['id'], expected_revision=wi['revision'], patch={'priority': 'urgent', 'owner_role': 'Builder2'})
    assert up['priority'] == 'urgent' and up['extensions']['owner_role'] == 'Builder2'


def test_dispatch_records_task_manifest_lease_and_intervention(env):
    _, wi = plan(env)
    r = env.call('acheron_dispatch', work_item_id=wi['id'], session='Builder', prompt='Add the /mcp route.\nRun cargo test.',
                 scopes=[{'selector': 'src-tauri/src/hub.rs', 'access': 'write'}])
    assert r['task_id'].startswith('task.') and len(r['lease_ids']) == 1 and r['sent_via'] == 'fake'
    sent = env.transport.sent[BUILDER][0]
    assert sent.startswith(f"[overseer → {r['task_id']} · Wire MCP endpoint]\nAdd the /mcp route.")
    st = env.store()
    t = st.get('task', r['task_id'])
    assert t['status'] == 'running' and t['context_manifest_id'] == r['manifest_id'] and t['lease_ids'] == r['lease_ids']
    assert st.get('session', BUILDER)['extensions']['active_task_id'] == r['task_id']
    assert st.get('work_item', wi['id'])['status'] == 'active'
    ev = [e for e in st.events if e['event_type'] == 'agent_intervention'][-1]
    assert ev['payload']['task_id'] == r['task_id'] and len(ev['payload']['content_digest']) == 64 and ev['payload']['via'] == 'fake'
    assert (env.root / 'manifests' / f"{r['manifest_id']}.json").exists()
    rejects(lambda: env.call('acheron_dispatch', work_item_id=wi['id'], session='Builder', prompt='again'), 'already has active task')


def test_overlapping_scopes_conflict_without_sending(env):
    _, wi = plan(env)
    env.call('acheron_dispatch', work_item_id=wi['id'], session='Builder', prompt='x', scopes=[{'selector': 'src-tauri/src/hub.rs', 'access': 'write'}])
    err = rejects(lambda: env.call('acheron_dispatch', work_item_id=wi['id'], session='Tester', prompt='also edit hub.rs',
                                   scopes=[{'selector': 'src-tauri/src/hub.rs', 'access': 'write'}]), 'CONFLICT')
    assert 'hub.rs' in err and 'Builder' in err
    assert TESTER not in env.transport.sent
    rejects(lambda: env.call('acheron_dispatch', work_item_id=wi['id'], session='Tester', prompt='x', scopes=[{'selector': 'src-tauri', 'access': 'write', 'recursive': True}]), 'CONFLICT')
    st = env.store()
    assert [t for t in st.list('task') if t['status'] == 'cancelled'], 'conflicting attempts are cancelled tasks'
    assert any(e['event_type'] == 'lease.conflict' for e in st.events)
    ok = env.call('acheron_dispatch', work_item_id=wi['id'], session='Tester', prompt='write tests only', scopes=[{'selector': 'tests/hub.rs', 'access': 'write'}])
    assert ok['task_id']
    env.call('acheron_checkpoint', task_id=ok['task_id'], result='succeeded', summary='tests written')


def test_policies_unwired_busy_paused_self_approval_error(env):
    rejects(lambda: env.call('acheron_intervene', session='Shell', content='hello'), 'policy.write_requires_wire')
    env.policy['user_typed_ago_ms'][TESTER] = 1000
    rejects(lambda: env.call('acheron_intervene', session='Tester', content='hello'), 'policy.busy')
    env.policy['user_typed_ago_ms'].pop(TESTER)
    env.transport.by(TESTER)['status'] = 'waiting'
    rejects(lambda: env.call('acheron_intervene', session='Tester', content='y'), 'policy.forward_approvals')
    r = env.call('acheron_intervene', session='Tester', content='Here is the API schema you asked for: {"id": "string"}')
    assert r['event_id'].startswith('evt') and 'API schema' in env.transport.sent[TESTER][-1]
    env.transport.by(TESTER)['status'] = 'idle'
    rejects(lambda: env.call('acheron_intervene', session='Overseer', content='hi'), 'policy.self')
    env.policy['paused'] = True
    rejects(lambda: env.call('acheron_intervene', session='Builder', content='hello'), 'PAUSED')
    assert env.call('acheron_fleet')['overseer_workspace']['paused'] is True
    env.policy['paused'] = False
    env.transport.by(TESTER)['status'] = 'error'
    rejects(lambda: env.call('acheron_intervene', session='Tester', content='hello'), 'policy.no_error_targets')
    env.transport.by(TESTER)['status'] = 'idle'
    env.call('acheron_interrupt', session='Builder', key='esc')
    assert env.transport.sent[BUILDER][-1] == '<esc>'


def test_checkpoint_releases_leases_and_never_completes_the_item(env):
    _, wi = plan(env)
    d = env.call('acheron_dispatch', work_item_id=wi['id'], session='Builder', prompt='x', scopes=[{'selector': 'src-tauri/src/hub.rs', 'access': 'write'}])
    r = env.call('acheron_checkpoint', task_id=d['task_id'], result='succeeded', summary='route added, tests pass',
                 changed_paths=['src-tauri/src/hub.rs'], validation=[{'command': 'cargo test', 'exit_code': 0}])
    assert r['status'] == 'succeeded' and 'unchanged' in r['note']
    st = env.store()
    assert all(lease['status'] != 'active' for lease in st.list('lease'))
    assert st.get('work_item', wi['id'])['status'] == 'active'
    assert st.get('session', BUILDER)['extensions']['active_task_id'] is None
    rejects(lambda: env.call('acheron_checkpoint', task_id=d['task_id'], result='succeeded', summary='again'), 'already succeeded')


def test_done_needs_evidence(env):
    _, wi = plan(env)
    d = env.call('acheron_dispatch', work_item_id=wi['id'], session='Builder', prompt='x')
    env.call('acheron_checkpoint', task_id=d['task_id'], result='succeeded', summary='done-ish')
    item = env.call('acheron_work_list', id=wi['id'])['items'][0]
    crit = item['criteria'][0]
    w = env.call('acheron_work_transition', id=wi['id'], expected_revision=item['revision'], event='submit')
    assert w['status'] == 'review'
    err = rejects(lambda: env.call('acheron_work_transition', id=wi['id'], expected_revision=w['revision'], event='pass'), 'unmet')
    assert crit['id'] in err
    ev = env.call('acheron_evidence_attach', work_item_id=wi['id'], criterion_id=crit['id'], task_id=d['task_id'], passed=True,
                  command='cargo test', exit_code=0, output='test result: ok. 19 passed')
    assert ev['status'] == 'passed' and ev['unmet_required'] == []
    w = env.call('acheron_work_transition', id=wi['id'], expected_revision=ev['work_item_revision'], event='pass')
    assert w['status'] == 'done'
    rejects(lambda: env.call('acheron_work_transition', id=wi['id'], expected_revision=w['revision'], event='start'), 'terminal')


def test_approval_criteria_open_a_gate(env):
    item = env.call('acheron_work_create', kind='task', title='Design sign-off',
                    acceptance_criteria=[{'statement': 'User approves the design', 'verifier': {'kind': 'approval'}}])
    env.call('acheron_work_transition', id=item['id'], expected_revision=item['revision'], event='ready')
    d = env.call('acheron_dispatch', work_item_id=item['id'], session='Builder', prompt='draft the design doc')
    env.call('acheron_checkpoint', task_id=d['task_id'], result='succeeded', summary='drafted')
    r = env.call('acheron_evidence_attach', work_item_id=item['id'], criterion_id=item['acceptance_criteria'][0]['id'], task_id=d['task_id'],
                 passed=True, summary='please review docs/design.md')
    assert r['pending'] is True
    st = env.store()
    gate = st.get('gate', r['gate_id'])
    assert gate['kind'] == 'approval' and gate['related']['criterion_id'] == item['acceptance_criteria'][0]['id']
    st.decide_gate(gate['id'], 'Approve')
    assert st.get('gate', gate['id'])['status'] == 'answered'


def test_ask_report_propose_decide_label_wait(env):
    _, wi = plan(env)
    d = env.call('acheron_dispatch', work_item_id=wi['id'], session='Builder', prompt='x')
    env.call('acheron_checkpoint', task_id=d['task_id'], result='succeeded', summary='ok')
    item = env.call('acheron_work_list', id=wi['id'])['items'][0]
    ev = env.call('acheron_evidence_attach', work_item_id=wi['id'], criterion_id=item['criteria'][0]['id'], task_id=d['task_id'], passed=True, output='ok')
    w = env.call('acheron_work_transition', id=wi['id'], expected_revision=ev['work_item_revision'], event='submit')
    env.call('acheron_work_transition', id=wi['id'], expected_revision=w['revision'], event='pass')
    q = env.call('acheron_ask_user', question='Drop mosh from scope?', options=['yes', 'no'])
    assert q['queued'] is True
    for _ in range(2):
        env.call('acheron_ask_user', question='more?')
    rejects(lambda: env.call('acheron_ask_user', question='too many'), 'already open')
    rep = env.call('acheron_report', summary='Hub endpoint done; tester idle.', health='green', risks=['none'], next='dispatch tests')
    assert rep['ok'] is True
    status = (env.root / 'PROJECT_STATUS.md').read_text()
    assert '# Project Status' in status and 'Wire MCP endpoint' in status and '[x]' in status
    assert (env.root / 'PLAN.md').exists() and json.loads((env.root / 'overseer.json').read_text())['report']['health'] == 'green'
    assert env.store().get_extension('report')['summary'].startswith('Hub endpoint')
    p = env.call('acheron_propose', kind='retire', title='Retire Tester', rationale='idle for hours', alternatives=['keep'], refs=[TESTER])
    assert p['status'] == 'proposed' and env.store().get('gate', p['gate_id'])['related']['proposal_id'] == p['proposal_id']
    dec = env.call('acheron_decide', what='Retire Tester', why='no owed work', topic='staffing', alternatives=['keep'])
    ki = env.store().get('knowledge_item', dec['knowledge_item_id'])
    assert ki['kind'] == 'decision' and ki['status'] == 'proposed' and 'Alternatives considered' in ki['body']
    lab = env.call('acheron_label', session='Tester', text='Reviewer')
    assert lab['callsign'] == 'Reviewer' and env.transport.labels[TESTER] == 'Reviewer'
    rejects(lambda: env.call('acheron_label', session='Shell', text='x'), 'write_requires_wire')
    r = env.call('acheron_wait', seconds=1)
    assert r['events'] == []
    # decisions and gates show up in the projection the surfaces render
    proj = env.store().projection()
    assert proj['decisions'] and [g for g in proj['gates'] if g['status'] == 'open']


def test_spawn_policies_allow_confirm_deny_cap(env):
    r = env.call('acheron_spawn', role='Reviewer', prompt='You review PRs.')
    assert r['session'] == 'session.reviewer' and r['wired'] is True
    assert env.transport.sent['session.reviewer'] == ['You review PRs.\n']
    assert env.store().get('session', 'session.reviewer')['extensions']['callsign'] == 'Reviewer'
    env.policy['spawn'] = 'confirm'
    g = env.call('acheron_spawn', role='Tester2', prompt='test things')
    assert g['pending'] is True and env.store().get('gate', g['gate_id'])['related']['action']['tool'] == 'acheron_spawn'
    env.policy['spawn'] = 'deny'
    rejects(lambda: env.call('acheron_spawn', role='X', prompt='x'), 'policy.spawn_denied')
    env.policy['spawn'] = 'allow'
    env.policy['max_blocks'] = len(env.transport.sessions)
    rejects(lambda: env.call('acheron_spawn', role='X', prompt='x'), 'policy.spawn_cap')


def test_lease_heartbeat_and_release(env):
    _, wi = plan(env)
    d = env.call('acheron_dispatch', work_item_id=wi['id'], session='Builder', prompt='x', scopes=[{'selector': 'a.rs', 'access': 'write'}])
    hb = env.call('acheron_lease_heartbeat', lease_id=d['lease_ids'][0])
    assert hb['expires_at']
    rel = env.call('acheron_lease_release', lease_id=d['lease_ids'][0], reason='done early')
    assert rel['status'] == 'released'


def test_rate_limit_and_journal(env, monkeypatch):
    monkeypatch.setattr(OT, 'MAX_MUTATIONS_PER_WINDOW', 3)
    for i in range(3):
        env.call('acheron_work_create', kind='task', title=f'item {i}')
    rejects(lambda: env.call('acheron_work_create', kind='task', title='one too many'), 'rate limit')
    env.call('acheron_fleet')  # reads still work
    j = [json.loads(line) for line in (env.root / 'journal.jsonl').read_text().splitlines()]
    assert any(x['name'] == 'acheron_work_create' and not x['ok'] for x in j) and j[-1]['name'] == 'acheron_fleet'


def test_opening_an_acheron_written_workspace_keeps_its_id(env):
    (env.root).mkdir(parents=True, exist_ok=True)
    from charon.workspace import WorkspaceStore
    WorkspaceStore.open(env.root, workspace_id='workspace.acheron.ws-42', replica_id='replica.acheron.x', slug='e2e', title='E2E')
    OT.reset_store_cache()
    st = env.store()
    assert st.workspace_id == 'workspace.acheron.ws-42' and st.workspace['title'] == 'E2E'
    r = env.call('acheron_fleet')
    assert r['overseer_workspace']['id'] == 'workspace.acheron.ws-42'


# ── MCP smoke: the server routes acheron_* to these executors ─────────────

ROOT = Path(__file__).resolve().parents[1]


def _rpc_lines(proc, lines):
    out = []
    for line in lines:
        proc.stdin.write(json.dumps(line) + '\n')
        proc.stdin.flush()
        if 'id' in line:
            out.append(json.loads(proc.stdout.readline()))
    return out


def test_mcp_overseer_profile_routes_to_executors(tmp_path):
    ws = tmp_path / 'ws'
    proj = tmp_path / 'proj'
    proj.mkdir()
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'CHARON_TMUX_SOCKET': f'charon-none-{uuid.uuid4().hex[:6]}',
           'TMUX_TMPDIR': str(tmp_path / 'tmux'), 'CHARON_SOCK': str(tmp_path / 'no-charond.sock')}
    (tmp_path / 'tmux').mkdir()
    proc = subprocess.Popen([sys.executable, '-m', 'charon.mcp_server', '--profile', 'overseer', '--workspace-root', str(ws),
                             '--project-root', str(proj), '--agent-id', 'overseer-test'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(ROOT))
    try:
        init, created, fleet, listed = _rpc_lines(proc, [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '0'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'acheron_work_create', 'arguments': {'kind': 'objective', 'title': 'Via MCP'}}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'acheron_fleet', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'acheron_work_list', 'arguments': {}}},
        ])
        assert init['result']['serverInfo']['name'] == 'charon'
        assert created['result']['isError'] is False
        rec = json.loads(created['result']['content'][0]['text'])
        assert rec['title'] == 'Via MCP' and rec['status'] == 'backlog'
        assert fleet['result']['isError'] is False
        f = json.loads(fleet['result']['content'][0]['text'])
        assert f['workspaces'][0]['sessions'] == [] and f['overseer_workspace']['transport'] == 'tmux'
        assert json.loads(listed['result']['content'][0]['text'])['count'] == 1
        assert (ws / 'records' / 'work_item.json').exists() and (ws / 'journal.jsonl').exists()
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
