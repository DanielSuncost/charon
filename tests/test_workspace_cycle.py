"""charon.workspace.cycle — digest collection, format, hold rules, delivery."""
from __future__ import annotations

import json
import re

from charon.workspace import cycle as C
from charon.workspace.store import WorkspaceStore

OV = {'kind': 'agent', 'id': 'agent.overseer.ws1'}
SYS = {'kind': 'system', 'id': 'system.charon'}


class FakeTransport:
    def __init__(self, sessions=None, screens=None, fg=None):
        self.sessions = sessions or []
        self.screens = screens or {}
        self.fg = fg or {}
        self.sent: list[tuple[str, str]] = []

    def list_sessions(self):
        return [dict(s) for s in self.sessions]

    def read(self, session_id, mode='last_message', lines=2000):
        return self.screens.get(session_id, '')

    def send(self, session_id, text, *, enter=True):
        self.sent.append((session_id, text))
        return 'fake'

    def interrupt(self, session_id, key='ctrl-c'):
        pass

    def foreground(self, session_id):
        return self.fg.get(session_id)


def _store(tmp_path, now=None):
    return WorkspaceStore.open(tmp_path / 'ws', workspace_id='workspace.charon.ws1', replica_id='replica.charon.test',
                               slug='ws1', title='Alpha', roots=[{'kind': 'directory', 'locator': str(tmp_path)}], now=now)


def _session(store, sid, callsign, role=None, status='idle'):
    return store.create('session', {'id': sid, 'kind': 'coordinator' if role == 'overseer' else 'worker', 'status': status,
                                    'actor': {'kind': 'agent', 'id': 'agent.' + sid[8:]}, 'host_ref': 'local', 'task_ids': [],
                                    'started_at': store.iso(), 'event_cursors': {},
                                    'extensions': {'callsign': callsign, 'title': callsign, 'role': role}}, actor=SYS)


def _populate(store):
    """Roughly what Acheron's dispatcher does: plan, dispatch, checkpoint, gate."""
    _session(store, 'session.ov', 'Overseer', role='overseer')
    _session(store, 'session.b1', 'Builder')
    wi = store.create('work_item', {'kind': 'task', 'title': 'Wire MCP endpoint', 'priority': 'high',
                                    'acceptance_criteria': [{'statement': 'cargo test passes', 'verifier': {'kind': 'command', 'spec': {'command': 'cargo test'}}}]}, actor=OV)
    wi = store.transition('work_item', wi['id'], wi['revision'], 'ready', actor=OV)
    run = store.create('run', {'work_item_ids': [wi['id']], 'task_ids': []}, actor=OV)
    task = store.create('task', {'work_item_id': wi['id'], 'run_id': run['id'], 'title': 'Wire MCP → Builder', 'instruction': 'do it',
                                 'session_id': 'session.b1', 'scopes': [{'selector': 'src/hub.rs', 'access': 'write'}]}, actor=OV)
    store.lease_acquire(task_id=task['id'], session_id='session.b1', scopes=[{'selector': 'src/hub.rs', 'access': 'write'}], actor=OV)
    store.transition('task', task['id'], store.get('task', task['id'])['revision'], 'start', actor=SYS)
    store.set_session_status('session.b1', 'active', actor=SYS, extensions={'active_task_id': task['id']})
    store.transition('work_item', wi['id'], store.get('work_item', wi['id'])['revision'], 'start', actor=SYS, reason='dispatched')
    # a second task on the same scope conflicts
    t2 = store.create('task', {'work_item_id': wi['id'], 'run_id': run['id'], 'title': 'also hub.rs', 'instruction': 'x', 'session_id': 'session.t1'}, actor=OV)
    _session(store, 'session.t1', 'Tester')
    try:
        store.lease_acquire(task_id=t2['id'], session_id='session.t1', scopes=[{'selector': 'src/hub.rs', 'access': 'write'}], actor=OV)
    except Exception:
        pass
    store.transition('task', t2['id'], store.get('task', t2['id'])['revision'], 'cancel', actor=SYS, reason='lease conflict')
    gate = store.create_gate(kind='question', question='Drop mosh from scope?', options=['yes', 'no'], actor=OV)
    store.decide_gate(gate['id'], 'yes')
    store.set_session_status('session.b1', 'idle', actor=SYS, extensions={'last_message_head': 'hub.rs done, tests pass'})
    store.transition('task', task['id'], store.get('task', task['id'])['revision'], 'succeed', actor=OV, reason='done')
    return wi, task


def test_collect_events_turns_workspace_events_into_acheron_worded_lines(tmp_path):
    store = _store(tmp_path)
    wi, task = _populate(store)
    lines, cursor, statuses = C.collect_events(store, None)
    texts = [entry['text'] for entry in lines]
    assert f'work item {wi["id"]} → ready' in texts
    assert f'work item {wi["id"]} → active' in texts
    assert 'lease conflict: Tester vs Builder on src/hub.rs' in texts
    assert 'gate question "Drop mosh from scope?" decided by user: yes' in texts
    assert f'Builder ({task["id"]}): finished — "hub.rs done, tests pass"' in texts
    assert f'task {task["id"]} (Builder) → succeeded' in texts
    assert not any('Overseer' in t for t in texts), 'the overseer\'s own transitions never become events'
    assert not any(t.startswith('record.') for t in texts)
    assert cursor == {'replica.charon.test': store.seq}
    assert statuses == {}
    # cursor semantics: nothing new the second time
    again, _, _ = C.collect_events(store, cursor)
    assert again == []


def test_collect_events_observes_session_status_changes_through_the_transport(tmp_path):
    store = _store(tmp_path)
    _session(store, 'session.ov', 'Overseer', role='overseer')
    _session(store, 'session.b1', 'Builder')
    store.set_extension(C.EXT_SESSION_STATUS, {'session.b1': 'running', 'session.t1': 'idle', 'session.ov': 'idle'})
    tr = FakeTransport(sessions=[
        {'id': 'session.b1', 'callsign': 'Builder', 'status': 'idle'},
        {'id': 'session.t1', 'callsign': 'Tester', 'status': 'waiting'},
        {'id': 'session.ov', 'callsign': 'Overseer', 'status': 'running', 'role': 'overseer'},
    ], screens={'session.b1': 'Wrote docs/design.md sk-ant-abcdefghijklmnopqrstuvwxyz1234 ✓ Done.', 'session.t1': 'Do you want to proceed? (y/n)'})
    lines, cursor, statuses = C.collect_events(store, {'replica.charon.test': store.seq}, transport=tr)
    texts = [entry['text'] for entry in lines]
    assert 'Builder: finished — "Wrote docs/design.md sk-ant…[redacted] ✓ Done."' in texts
    assert 'Tester: waiting for approval — "Do you want to proceed? (y/n)"' in texts
    assert not any('Overseer' in t for t in texts)
    assert statuses == {'session.b1': 'idle', 'session.t1': 'waiting', 'session.ov': 'running'}


def test_build_digest_matches_acheron_format(tmp_path):
    store = _store(tmp_path, now=lambda: 1_756_942_200_000.0)  # fixed clock
    _populate(store)
    lines = [{'at': 1_756_942_200_000.0 - 26_000, 'kind': 'finished', 'text': 'Builder: finished — "ok"'},
             {'at': 1_756_942_200_000.0 - 90_000, 'kind': 'gate', 'text': 'gate question "Q?" decided by user: yes'}]
    text = C.build_digest(store, 7, 'Alpha', lines, now=lambda: 1_756_942_200_000.0)
    head, *body, tail = text.split('\n')
    assert re.fullmatch(r'\[acheron cycle 7 · \d\d:\d\d · workspace Alpha · 0 active tasks · 0 gates open\]', head), head
    assert body == ['- Builder: finished — "ok" (26s ago)', '- gate question "Q?" decided by user: yes (2m ago)']
    assert tail == 'Run a cycle.'
    empty = C.build_digest(store, 8, 'Alpha', [], now=lambda: 1_756_942_200_000.0)
    assert '- (no new events)' in empty and '1 active task ·' not in empty


def test_digest_counts_active_tasks_and_open_gates_with_singular_forms(tmp_path):
    store = _store(tmp_path)
    _session(store, 'session.b1', 'Builder')
    wi = store.create('work_item', {'kind': 'task', 'title': 'x'}, actor=OV)
    run = store.create('run', {'work_item_ids': [wi['id']], 'task_ids': []}, actor=OV)
    store.create('task', {'work_item_id': wi['id'], 'run_id': run['id'], 'title': 't', 'instruction': 'i', 'session_id': 'session.b1'}, actor=OV)
    store.create_gate(kind='question', question='?', actor=OV)
    text = C.build_digest(store, 1, 'Alpha', [])
    assert '· 1 active task · 1 gate open]' in text


def test_run_cycle_delivers_records_cycle_started_and_advances_cursor(tmp_path):
    store = _store(tmp_path)
    _populate(store)
    delivered = []
    r = C.run_cycle(store, deliver=lambda t: delivered.append(t) or True, workspace_name='Alpha')
    assert r['delivered'] and r['cycle'] == 1 and r['held'] is None
    assert delivered and delivered[0].startswith('[acheron cycle 1 · ')
    assert delivered[0].endswith('Run a cycle.')
    assert store.get_extension(C.EXT_CYCLE_COUNT) == 1
    ev = store.events[-1]
    assert ev['event_type'] == 'cycle.started' and ev['payload']['cycle'] == 1 and ev['payload']['lines'] == r['lines']
    assert store.verify_chain()['ok']
    # nothing new → held, no delivery, cycle count unchanged
    r2 = C.run_cycle(store, deliver=lambda t: delivered.append(t) or True)
    assert not r2['delivered'] and r2['held'] == 'no new events' and len(delivered) == 1
    # force delivers an empty digest
    r3 = C.run_cycle(store, deliver=lambda t: delivered.append(t) or True, force=True)
    assert r3['delivered'] and '- (no new events)' in delivered[-1] and r3['cycle'] == 2


def test_failed_delivery_keeps_the_events_for_the_next_attempt(tmp_path):
    store = _store(tmp_path)
    _populate(store)
    r = C.run_cycle(store, deliver=lambda t: False)
    assert not r['delivered'] and r['held'] == 'delivery failed' and r['lines']
    assert store.get_extension(C.EXT_CYCLE_COUNT) in (None, 0)
    assert not any(e['event_type'] == 'cycle.started' for e in store.events)
    boom = C.run_cycle(store, deliver=lambda t: (_ for _ in ()).throw(RuntimeError('pty dead')))
    assert boom['held'] == 'delivery failed'
    ok = C.run_cycle(store, deliver=lambda t: True)
    assert ok['delivered'] and ok['lines'] == r['lines']


def test_hold_rules_shell_foreground_busy_typing_and_hourly_cap(tmp_path):
    store = _store(tmp_path)
    _populate(store)
    tr = FakeTransport(sessions=[{'id': 'session.ov', 'callsign': 'Overseer', 'status': 'idle', 'role': 'overseer'}], fg={'session.ov': 'zsh'})
    r = C.run_cycle(store, deliver=lambda t: True, transport=tr, overseer_session_id='session.ov')
    assert not r['delivered'] and r['held'].startswith('overseer offline')
    tr.fg['session.ov'] = '2.1.259'  # the claude launcher's process name is a bare version string
    tr.sessions[0]['status'] = 'running'
    assert C.run_cycle(store, deliver=lambda t: True, transport=tr, overseer_session_id='session.ov')['held'] == 'overseer busy'
    tr.sessions[0]['status'] = 'idle'
    tr.sessions[0]['user_typed_ago_ms'] = 3000
    assert C.run_cycle(store, deliver=lambda t: True, transport=tr, overseer_session_id='session.ov')['held'] == 'user typing in the overseer session'
    del tr.sessions[0]['user_typed_ago_ms']
    now = 1_756_942_200_000.0
    store.set_extension(C.EXT_STAMPS, [now - 1000 * i for i in range(1, 13)])
    r = C.run_cycle(store, deliver=lambda t: True, transport=tr, overseer_session_id='session.ov', cadence={'max_cycles_per_hour': 12}, now=lambda: now)
    assert r['held'].startswith('hourly cap')
    r = C.run_cycle(store, deliver=lambda t: True, transport=tr, overseer_session_id='session.ov', cadence={'max_cycles_per_hour': 13}, now=lambda: now)
    assert r['delivered']
    assert store.get_extension(C.EXT_SESSION_STATUS) == {'session.ov': 'idle'}


def test_deliver_to_session_and_to_agent(tmp_path):
    store = _store(tmp_path)
    _populate(store)
    tr = FakeTransport()
    r = C.run_cycle(store, deliver=C.deliver_to_session(tr, 'session.ov'))
    assert r['delivered'] and tr.sent[0][0] == 'session.ov' and tr.sent[0][1] == r['text']
    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    r2 = C.run_cycle(store, deliver=C.deliver_to_agent(state_dir, 'AG-OV', project='/proj'), force=True)
    assert r2['delivered']
    queue = json.loads((state_dir / 'queue.json').read_text())
    task = [t for t in queue if t.get('task_type') == 'agent_task'][0]
    assert task['owner_agent_id'] == 'AG-OV' and task['title'] == 'overseer cycle'
    assert task['instruction'].startswith('[acheron cycle 2 ·') and task['instruction'].endswith('Run a cycle.\nFollow the overseer skill.')


def test_build_transport_none_and_missing_module_are_safe():
    assert C.build_transport(None) is None
    assert C.build_transport({'kind': 'none'}) is None
    # 'auto'/'tmux' return a transport only when charon.workspace.transport exists; never raise
    C.build_transport({'kind': 'auto'})
