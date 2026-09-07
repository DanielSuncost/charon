"""WorkspaceStore: lifecycles, revisions, events, leases, artifacts, gates, projections."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from charon.workspace import (
    EventCollision, GuardFailed, IllegalTransition, LeaseConflict, NotFound, RevisionConflict, ValidationError,
    WorkspaceStore, event_hash, canonical_json, sha256_hex,
)
from charon.workspace.machines import SPECS
from charon.workspace.records import FSM, iso_from_ms

OV = {'kind': 'agent', 'id': 'agent.overseer.t'}


class Clock:
    def __init__(self, start_ms: float = 1_756_936_800_000.0):
        self.t = start_ms

    def __call__(self) -> float:
        self.t += 1000.0
        return self.t


def _open(root: Path, clock: Clock | None = None, replica: str = 'replica.test.a') -> WorkspaceStore:
    return WorkspaceStore.open(root, workspace_id='workspace.test.one', replica_id=replica, slug='one', title='One',
                               roots=[{'kind': 'directory', 'locator': '/proj'}], now=clock or Clock())


def _events(root: Path, replica: str = 'replica.test.a') -> list[dict]:
    path = root / 'events' / f'{replica}.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _item(ws: WorkspaceStore, **extra) -> dict:
    fields = {'kind': 'task', 'title': 'Wire endpoint', 'scopes': [{'selector': 'src/hub.rs'}],
              'acceptance_criteria': [{'statement': 'cargo test passes', 'verifier': {'kind': 'command', 'spec': {'command': 'cargo test'}}}],
              'owner_role': 'Builder'}
    fields.update(extra)
    return ws.create('work_item', fields, actor=OV)


def _task(ws: WorkspaceStore, wi: dict, session: str = 'session.b', **extra) -> dict:
    run = ws.list('run', lambda r: wi['id'] in r['work_item_ids'])
    run = run[0] if run else ws.create('run', {'work_item_ids': [wi['id']]}, actor=OV)
    fields = {'work_item_id': wi['id'], 'run_id': run['id'], 'title': 'do it', 'instruction': 'do it', 'session_id': session}
    fields.update(extra)
    return ws.create('task', fields, actor=OV)


# ── bootstrap / persistence ─────────────────────────────────────────────────

def test_open_bootstraps_workspace_and_replica(tmp_path):
    ws = _open(tmp_path)
    assert ws.workspace['record_type'] == 'workspace'
    assert ws.workspace['slug'] == 'one' and ws.workspace['home_replica_id'] == 'replica.test.a'
    assert ws.workspace['extensions']['acheron']['fencing_counters'] == {}
    assert ws.get('replica', 'replica.test.a')['identity'] == 'charon:replica.test.a'
    assert [e['event_type'] for e in ws.events] == ['record.created', 'record.created']
    assert (tmp_path / 'workspace.json').exists()
    assert (tmp_path / 'records' / 'replica.json').exists()
    assert (tmp_path / 'events' / 'replica.test.a.jsonl').exists()
    assert ws.verify_chain() == {'ok': True, 'count': 2, 'broken_at': None, 'reason': None}


def test_timestamps_match_js_toisostring(tmp_path):
    assert iso_from_ms(1_756_936_801_000.0) == '2025-09-03T22:00:01.000Z'
    assert iso_from_ms(1_756_936_801_123.0) == '2025-09-03T22:00:01.123Z'
    ws = _open(tmp_path, Clock())
    assert ws.workspace['created_at'].endswith('Z') and len(ws.workspace['created_at']) == 24


def test_create_moves_unknown_fields_to_extensions_and_applies_defaults(tmp_path):
    ws = _open(tmp_path)
    wi = _item(ws)
    assert wi['status'] == 'backlog' and wi['priority'] == 'normal' and wi['owner'] == OV
    assert wi['extensions'] == {'owner_role': 'Builder'}
    assert wi['scopes'] == [{'kind': 'path', 'selector': 'src/hub.rs', 'access': 'write'}]
    c = wi['acceptance_criteria'][0]
    assert c['id'].startswith('criterion.') and c['required'] is True and c['status'] == 'pending' and c['evidence_artifact_ids'] == []
    assert wi['revision'] == 1 and wi['record_type'] == 'work_item'
    ev = ws.events[-1]
    assert ev['event_type'] == 'record.created' and ev['subject_refs'][0]['id'] == wi['id']
    assert ev['payload']['record_digest'] == sha256_hex(canonical_json(wi))
    with pytest.raises(ValidationError):
        ws.create('work_item', {'title': 'x', 'kind': 'bogus'})
    with pytest.raises(ValidationError):
        ws.create('work_item', {'id': wi['id'], 'title': 'dup'})
    with pytest.raises(ValidationError):
        ws.create('run', {'work_item_ids': []})


# ── transitions ─────────────────────────────────────────────────────────────

def test_machines_cover_every_table_row():
    for record_type, table in FSM.items():
        spec = SPECS[record_type]
        for source, row in table.items():
            for event, target in row.items():
                t = spec.transition_for(source, event)
                assert t is not None and t.target == target, (record_type, source, event)
            if not row:
                assert source in spec.terminal_states


def test_legal_illegal_terminal_and_unknown_transitions(tmp_path):
    ws = _open(tmp_path)
    wi = _item(ws)
    wi = ws.transition('work_item', wi['id'], wi['revision'], 'ready', actor=OV)
    assert wi['status'] == 'ready' and wi['revision'] == 2
    ev = ws.events[-1]
    assert ev['event_type'] == 'record.transitioned' and ev['payload'] == {'record_type': 'work_item', 'from': 'backlog', 'to': 'ready', 'event': 'ready', 'reason': None}
    assert ev['base_revision'] == 1 and ev['subject_refs'][0]['revision'] == 2
    with pytest.raises(IllegalTransition, match='not legal'):
        ws.transition('work_item', wi['id'], wi['revision'], 'pass')
    with pytest.raises(IllegalTransition):
        ws.transition('work_item', wi['id'], wi['revision'], 'bogus-event')
    wi = ws.transition('work_item', wi['id'], wi['revision'], 'cancel', reason='scope cut', actor=OV)
    assert wi['status'] == 'cancelled' and wi['terminal_reason'] == 'scope cut'
    with pytest.raises(IllegalTransition, match='terminal'):
        ws.transition('work_item', wi['id'], wi['revision'], 'ready')
    with pytest.raises(ValidationError):
        ws.transition('artifact', 'artifact.x', 1, 'x')
    with pytest.raises(NotFound):
        ws.transition('work_item', 'work.missing', 1, 'ready')


def test_task_lease_proposal_gate_side_effects(tmp_path):
    clock = Clock()
    ws = _open(tmp_path, clock)
    wi = _item(ws)
    task = _task(ws, wi)
    assert task['status'] == 'claimed' and task['attempt'] == 1 and task['idempotency_key'] == task['id']
    task = ws.transition('task', task['id'], task['revision'], 'start')
    task = ws.transition('task', task['id'], task['revision'], 'succeed', reason='all green', actor=OV)
    assert task['status'] == 'succeeded' and task['result_summary'] == 'all green' and task['completed_at'].endswith('Z')
    prop = ws.create_proposal(kind='staffing', title='Add reviewer', rationale='no review', actor=OV)
    prop = ws.transition_proposal(prop['id'], 'accept', reason='agreed')
    assert prop['status'] == 'accepted' and prop['decided_by'] == {'kind': 'user', 'id': 'user.local'} and prop['decision_reason'] == 'agreed'
    prop = ws.transition_proposal(prop['id'], 'apply', {'kind': 'system', 'id': 'system.charon'})
    assert prop['status'] == 'applied'
    gate = ws.create_gate(kind='question', question='Drop mosh?', options=['yes', 'no'], actor=OV)
    assert ws.events[-1]['event_type'] == 'gate.opened'
    gate = ws.decide_gate(gate['id'], 'yes')
    assert gate['status'] == 'answered' and gate['answer'] == {'answer': 'yes'} and gate['decided_by']['kind'] == 'user'
    assert ws.events[-1]['event_type'] == 'gate.decided'
    ki = ws.create('knowledge_item', {'kind': 'decision', 'title': 'Use tmux', 'body': 'because'}, actor=OV)
    assert ki['status'] == 'proposed' and ki['review'] == {'required': True, 'status': 'pending'} and ki['confidence'] == 0.5
    ki = ws.transition('knowledge_item', ki['id'], ki['revision'], 'verify')
    assert ki['status'] == 'verified'


def test_revision_cas_and_status_via_update_rejected(tmp_path):
    ws = _open(tmp_path)
    wi = _item(ws)
    with pytest.raises(RevisionConflict) as exc:
        ws.transition('work_item', wi['id'], 99, 'ready')
    assert exc.value.revision == 1
    with pytest.raises(RevisionConflict):
        ws.update('work_item', wi['id'], 5, {'title': 'x'})
    with pytest.raises(ValidationError, match='transition'):
        ws.update('work_item', wi['id'], 1, {'status': 'done'})
    wi = ws.update('work_item', wi['id'], 1, {'title': 'Renamed', 'priority': 'high', 'owner_role': 'Tester', 'extensions': {'x': 1}}, actor=OV)
    assert wi['title'] == 'Renamed' and wi['priority'] == 'high' and wi['extensions'] == {'owner_role': 'Tester', 'x': 1} and wi['revision'] == 2
    assert ws.events[-1]['payload']['changed'] == ['title', 'priority', 'owner_role', 'extensions']


def test_criteria_merge_by_id(tmp_path):
    ws = _open(tmp_path)
    wi = _item(ws)
    cid = wi['acceptance_criteria'][0]['id']
    wi = ws.update('work_item', wi['id'], wi['revision'], {'acceptance_criteria': [
        {'id': cid, 'required': False},
        {'statement': 'lint clean', 'verifier': {'kind': 'inspection'}},
    ]})
    assert len(wi['acceptance_criteria']) == 2
    kept, new = wi['acceptance_criteria']
    assert kept['id'] == cid and kept['statement'] == 'cargo test passes' and kept['required'] is False
    assert kept['verifier'] == {'kind': 'command', 'spec': {'command': 'cargo test'}}
    assert new['id'].startswith('criterion.') and new['verifier'] == {'kind': 'inspection', 'spec': {}}


# ── evidence-gated completion ──────────────────────────────────────────────

def test_pass_guard_requires_evidence_or_waiver(tmp_path):
    ws = _open(tmp_path)
    wi = _item(ws)
    cid = wi['acceptance_criteria'][0]['id']
    for ev in ('ready', 'start', 'submit'):
        wi = ws.transition('work_item', wi['id'], wi['revision'], ev, actor=OV)
    assert wi['status'] == 'review'
    with pytest.raises(GuardFailed) as exc:
        ws.transition('work_item', wi['id'], wi['revision'], 'pass', actor=OV)
    assert exc.value.unmet == [cid] and 'cannot pass' in str(exc.value)
    assert ws.get('work_item', wi['id'])['status'] == 'review' and ws.get('work_item', wi['id'])['revision'] == wi['revision']
    # attaching "passed" without evidence is refused
    with pytest.raises(ValidationError):
        ws.attach_evidence(wi['id'], wi['revision'], cid, status='passed')
    task = _task(ws, wi)
    art = ws.put_artifact(text='test result: ok', kind='test_result', produced_by=OV, task_id=task['id'])
    wi = ws.attach_evidence(wi['id'], wi['revision'], cid, status='passed', artifact_ids=[art['id']], task_id=task['id'], actor=OV)
    assert wi['acceptance_criteria'][0]['status'] == 'passed' and wi['acceptance_criteria'][0]['evidence_artifact_ids'] == [art['id']]
    assert ws.get('task', task['id'])['evidence_artifact_ids'] == [art['id']]
    assert ws.events[-1]['event_type'] == 'evidence.attached'
    wi = ws.transition('work_item', wi['id'], wi['revision'], 'pass', actor=OV)
    assert wi['status'] == 'done'


def test_pass_guard_accepts_waiver_with_reason(tmp_path):
    ws = _open(tmp_path)
    wi = _item(ws)
    cid = wi['acceptance_criteria'][0]['id']
    for ev in ('ready', 'start', 'submit'):
        wi = ws.transition('work_item', wi['id'], wi['revision'], ev)
    with pytest.raises(ValidationError):
        ws.attach_evidence(wi['id'], wi['revision'], cid, status='waived')
    wi = ws.attach_evidence(wi['id'], wi['revision'], cid, status='waived', waiver_reason='covered by CI')
    wi = ws.transition('work_item', wi['id'], wi['revision'], 'pass')
    assert wi['status'] == 'done' and wi['acceptance_criteria'][0]['waiver_reason'] == 'covered by CI'
    with pytest.raises(NotFound):
        ws.attach_evidence(wi['id'], wi['revision'], 'criterion.nope', status='failed')


# ── events: chain, idempotency, collision ───────────────────────────────────

def test_event_chain_links_and_tamper_detection(tmp_path):
    ws = _open(tmp_path)
    _item(ws)
    ws.append_event('cycle.report', actor=OV, payload={'summary': 'fine', 'health': 'green'})
    events = _events(tmp_path)
    assert events[0]['previous_digest'] is None
    for prev, cur in zip(events, events[1:], strict=False):
        assert cur['previous_digest'] == prev['digest']
        assert cur['replica_sequence'] == prev['replica_sequence'] + 1
    for e in events:
        assert event_hash(e) == e['digest']['value']
    assert ws.verify_chain()['ok'] is True and ws.verify_chain()['count'] == len(events)
    # tamper with a payload on disk
    path = tmp_path / 'events' / 'replica.test.a.jsonl'
    lines = path.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered['payload']['title'] = 'evil'
    lines[1] = json.dumps(tampered, ensure_ascii=False)
    path.write_text('\n'.join(lines) + '\n')
    result = ws.verify_chain()
    assert result['ok'] is False and result['broken_at'] == 2 and result['reason'] == 'digest mismatch'


def test_event_id_idempotent_replay_and_collision(tmp_path):
    ws = _open(tmp_path)
    a = ws.append_event('decision', actor=OV, event_id='evt.fixed-1', payload={'what': 'x'})
    b = ws.append_event('decision', actor=OV, event_id='evt.fixed-1', payload={'what': 'x'})
    assert a is b and sum(1 for e in ws.events if e['id'] == 'evt.fixed-1') == 1
    with pytest.raises(EventCollision):
        ws.append_event('decision', actor=OV, event_id='evt.fixed-1', payload={'what': 'y'})
    wi = _item(ws)
    wi2 = ws.transition('work_item', wi['id'], 1, 'ready', event_id='evt.tr-1')
    replay = ws.transition('work_item', wi['id'], 1, 'ready', event_id='evt.tr-1')  # stale revision, same content → no-op
    assert replay['revision'] == wi2['revision'] == 2 and replay['status'] == 'ready'
    with pytest.raises(EventCollision):
        ws.transition('work_item', wi['id'], 1, 'ready', event_id='evt.tr-1', reason='different')
    # across reopen
    ws2 = _open(tmp_path)
    again = ws2.append_event('decision', actor=OV, event_id='evt.fixed-1', payload={'what': 'x'})
    assert again['id'] == 'evt.fixed-1' and len(ws2.events) == len(ws.events)
    with pytest.raises(EventCollision):
        ws2.append_event('decision', actor=OV, event_id='evt.fixed-1', payload={'what': 'z'})
    assert ws2.transition('work_item', wi['id'], 1, 'ready', event_id='evt.tr-1')['status'] == 'ready'


def test_event_shape_and_causation_fields(tmp_path):
    ws = _open(tmp_path)
    ev = ws.append_event('agent_intervention', actor=OV, subject_refs=[{'record_type': 'session', 'id': 'session.b', 'extra': 'dropped'}],
                         payload={'content_head': 'hi'}, correlation_id='session.b', causation_id='evt.cause-1', base_revision=3)
    assert ev['record_type'] == 'event' and ev['workspace_id'] == 'workspace.test.one' and ev['replica_id'] == 'replica.test.a'
    assert ev['logical_time'] == ev['replica_sequence'] and ev['occurred_at'] == ev['created_at']
    assert ev['subject_refs'] == [{'record_type': 'session', 'id': 'session.b'}]
    assert ev['correlation_id'] == 'session.b' and ev['causation_event_id'] == 'evt.cause-1' and ev['base_revision'] == 3
    assert ev['digest']['algorithm'] == 'sha256' and len(ev['digest']['value']) == 64
    with pytest.raises(ValidationError):
        ws.append_event('Bad Type!')


# ── leases ──────────────────────────────────────────────────────────────────

def test_lease_overlap_rules_and_conflict_event(tmp_path):
    ws = _open(tmp_path)
    wi = _item(ws)
    t1 = _task(ws, wi, 'session.b')
    t2 = _task(ws, wi, 'session.c')
    leases = ws.lease_acquire(task_id=t1['id'], session_id='session.b', scopes=[{'selector': 'src/hub', 'recursive': True}], actor=OV)
    assert len(leases) == 1 and leases[0]['fencing_token'] == 1 and leases[0]['owner_session_id'] == 'session.b'
    assert ws.get('task', t1['id'])['lease_ids'] == [leases[0]['id']]
    # exact + recursive prefix (both directions)
    with pytest.raises(LeaseConflict):
        ws.lease_acquire(task_id=t2['id'], scopes=[{'selector': 'src/hub'}])
    with pytest.raises(LeaseConflict) as exc:
        ws.lease_acquire(task_id=t2['id'], scopes=[{'selector': 'src/hub/mcp.rs'}])
    assert exc.value.conflicts[0]['lease']['id'] == leases[0]['id'] and exc.value.conflicts[0]['scope']['selector'] == 'src/hub/mcp.rs'
    assert ws.events[-1]['event_type'] == 'lease.conflict' and ws.events[-1]['payload']['conflicts'][0]['lease_id'] == leases[0]['id']
    with pytest.raises(LeaseConflict):
        ws.lease_acquire(task_id=t2['id'], scopes=[{'selector': 'src', 'recursive': True}])
    # sibling path: no conflict
    ok = ws.lease_acquire(task_id=t2['id'], scopes=[{'selector': 'src/hubble.rs'}])
    assert ok[0]['status'] == 'active' and ok[0]['fencing_token'] == 1
    # shared + shared coexist; exclusive vs shared conflicts
    t3 = _task(ws, wi, 'session.d')
    t4 = _task(ws, wi, 'session.e')
    ws.lease_acquire(task_id=t3['id'], scopes=[{'selector': 'docs/plan.md'}], mode='shared')
    ws.lease_acquire(task_id=t4['id'], scopes=[{'selector': 'docs/plan.md'}], mode='shared')
    with pytest.raises(LeaseConflict):
        ws.lease_acquire(task_id=t2['id'], scopes=[{'selector': 'docs/plan.md'}], mode='exclusive')
    # the holder re-acquiring keeps its lease
    same = ws.lease_acquire(task_id=t1['id'], scopes=[{'selector': 'src/hub', 'recursive': True}])
    assert same[0]['id'] == leases[0]['id']
    with pytest.raises(ValidationError):
        ws.lease_acquire(task_id=t1['id'], scopes=[{'selector': 'x'}], mode='bogus')


def test_fencing_tokens_heartbeat_expire_release(tmp_path):
    clock = Clock()
    ws = _open(tmp_path, clock)
    wi = _item(ws)
    t1 = _task(ws, wi, 'session.b')
    t2 = _task(ws, wi, 'session.c')
    l1 = ws.lease_acquire(task_id=t1['id'], scopes=[{'selector': 'a.rs'}], ttl_ms=5_000)[0]
    assert l1['fencing_token'] == 1
    hb = ws.lease_heartbeat(l1['id'], ttl_ms=5_000)
    assert hb['revision'] == 2 and hb['expires_at'] > l1['acquired_at']
    events_before = len(ws.events)
    ws.lease_heartbeat(l1['id'])
    assert len(ws.events) == events_before  # liveness is not an event
    clock.t += 60 * 60 * 1000  # past expiry
    expired = ws.expire_leases()
    assert [lease['id'] for lease in expired] == [l1['id']] and ws.get('lease', l1['id'])['status'] == 'expired'
    assert ws.get('lease', l1['id'])['release_reason'] == 'ttl elapsed'
    with pytest.raises(IllegalTransition):
        ws.lease_heartbeat(l1['id'])
    l2 = ws.lease_acquire(task_id=t2['id'], scopes=[{'selector': 'a.rs'}])[0]
    assert l2['fencing_token'] == 2  # monotonic per selector
    assert ws.workspace['extensions']['acheron']['fencing_counters'] == {'a.rs': 2}
    rel = ws.lease_release(l2['id'], 'checkpoint')
    assert rel['status'] == 'released' and rel['release_reason'] == 'checkpoint' and rel['released_at']
    # counters persist across reopen
    ws2 = _open(tmp_path, clock)
    assert ws2.workspace['extensions']['acheron']['fencing_counters'] == {'a.rs': 2}


# ── artifacts / sessions ────────────────────────────────────────────────────

def test_artifact_idempotency_and_read(tmp_path):
    ws = _open(tmp_path)
    a = ws.put_artifact(text='hello world', kind='log', title='capture', produced_by=OV, session_id='session.b', metadata={'mode': 'screen'})
    assert a['id'] == f"artifact.sha256:{sha256_hex('hello world')}" and a['size_bytes'] == 11 and a['immutable'] is True
    assert a['locator'] == f"workspace/artifacts/sha256/{a['digest']['value']}"
    assert (tmp_path / 'artifacts' / 'sha256' / a['digest']['value']).read_text() == 'hello world'
    assert ws.read_artifact(a['id']) == 'hello world'
    events = len(ws.events)
    b = ws.put_artifact(text='hello world', kind='document')
    assert b is a and len(ws.events) == events
    assert ws.read_artifact('artifact.sha256:missing') is None
    with pytest.raises(ValidationError):
        ws.put_artifact()
    manifest = ws.create('context_manifest', {'task_id': 'task.x', 'entries': [{'record_ref': {'record_type': 'artifact', 'id': a['id']},
                         'section': 'prompt', 'order': 0, 'score': 1, 'score_components': {}, 'reasons': [], 'representation': 'inline',
                         'digest': a['digest'], 'units': 11}]}, actor=OV)
    assert (tmp_path / 'manifests' / f"{manifest['id']}.json").exists()
    assert manifest['digest']['value'] == sha256_hex(canonical_json(manifest['entries']))


def test_session_status_events_only_on_change(tmp_path):
    ws = _open(tmp_path)
    s = ws.create('session', {'id': 'session.b', 'kind': 'worker', 'actor': {'kind': 'agent', 'id': 'agent.b'}, 'host_ref': 'local',
                              'callsign': 'Builder'}, actor=OV)
    assert s['status'] == 'starting' and s['extensions'] == {'callsign': 'Builder'} and s['replica_id'] == 'replica.test.a'
    s = ws.set_session_status('session.b', 'active', extensions={'active_task_id': 'task.1'})
    assert s['status'] == 'active' and s['revision'] == 2 and ws.events[-1]['event_type'] == 'session.status'
    n = len(ws.events)
    s = ws.set_session_status('session.b', 'active', extensions={'note': 1})
    assert len(ws.events) == n and s['revision'] == 2 and s['extensions']['note'] == 1
    s = ws.set_session_status('session.b', 'stopped')
    assert s['ended_at'] and s['status'] == 'stopped'
    with pytest.raises(ValidationError):
        ws.set_session_status('session.b', 'bogus')


# ── extensions / reopen / projection ────────────────────────────────────────

def test_extensions_persist_and_reopen_continuity(tmp_path):
    clock = Clock()
    ws = _open(tmp_path, clock)
    ws.set_extension('overseer', {'cycle': 3, 'paused': False})
    wi = _item(ws)
    ws.transition('work_item', wi['id'], 1, 'ready')
    seq = ws.seq
    ws2 = _open(tmp_path, clock)
    assert ws2.get_extension('overseer') == {'cycle': 3, 'paused': False}
    assert ws2.seq == seq and len(ws2.events) == seq and ws2.get('work_item', wi['id'])['status'] == 'ready'
    ws2.append_event('cycle.started', payload={'cycle': 4})
    assert ws2.seq == seq + 1
    assert ws2.verify_chain()['count'] == seq + 1
    # a second replica opening the same directory starts its own chain and sees the history
    ws3 = _open(tmp_path, clock, replica='replica.test.b')
    assert len(ws3.events) == seq + 1 and ws3.seq == 0
    ws3.append_event('cycle.started', payload={'cycle': 5})
    assert (tmp_path / 'events' / 'replica.test.b.jsonl').exists() and ws3.verify_chain()['count'] == 1
    assert ws3.verify_chain('replica.test.a')['count'] == seq + 1


def test_projection_shape_and_status_documents(tmp_path):
    clock = Clock()
    ws = _open(tmp_path, clock)
    ws.create('session', {'id': 'session.ov', 'kind': 'coordinator', 'callsign': 'Overseer', 'role': 'overseer', 'agent': 'claude', 'acheron_status': 'idle'})
    ws.create('session', {'id': 'session.b', 'kind': 'worker', 'callsign': 'Builder', 'agent': 'claude', 'acheron_status': 'running'})
    obj = ws.create('work_item', {'kind': 'objective', 'title': 'Green tests'}, actor=OV)
    wi = _item(ws, parent_id=obj['id'])
    task = _task(ws, wi, 'session.b')
    ws.lease_acquire(task_id=task['id'], session_id='session.b', scopes=[{'selector': 'src/hub.rs'}])
    ws.create_gate(question='Ship it?', options=['yes', 'no'], actor=OV)
    ws.create('knowledge_item', {'kind': 'decision', 'title': 'Verify by command', 'body': 'trust nothing', 'topic': 'process'}, actor=OV)
    ws.set_extension('overseer', {'name': 'E2E', 'cycle': 2, 'paused': False,
                                  'report': {'summary': 'On track.', 'health': 'green', 'risks': ['flaky CI'], 'next': 'dispatch tests'}})
    p = ws.projection()
    assert set(p) == {'generated_at', 'workspace', 'sessions', 'work_items', 'runs', 'tasks', 'leases', 'gates', 'proposals', 'decisions', 'recent_events', 'counts'}
    assert p['workspace']['slug'] == 'one' and p['sessions'][0]['callsign'] == 'Overseer' and p['sessions'][1]['acheron_status'] == 'running'
    root = p['work_items'][0]
    assert root['id'] == obj['id'] and root['children'][0]['id'] == wi['id']
    child = root['children'][0]
    assert child['owner_role'] == 'Builder' and child['criteria'][0]['verifier_kind'] == 'command' and child['criteria'][0]['evidence_count'] == 0
    assert child['progress'] == {'required': 1, 'met': 0, 'total': 1, 'percent': 0} and child['tasks'][0]['id'] == task['id']
    assert p['leases'][0]['live'] is True and p['gates'][0]['status'] == 'open' and p['decisions'][0]['topic'] == 'process'
    assert p['recent_events'][-1]['payload_head'] and p['counts']['events'] == len(ws.events)
    ws.write_projections()
    status = (tmp_path / 'PROJECT_STATUS.md').read_text()
    for section in ('# Project Status: E2E', '## Summary', 'On track.', '**Next:** dispatch tests', '## Staffing', '**Overseer** (claude, overseer) — idle',
                    '**Builder** (claude) — running', '## Work Division', '**Builder**: 1 task (0 succeeded, 0 failed) · now: do it', '## Goals',
                    '- [ ] Green tests — backlog', '  - [ ] Wire endpoint @Builder — backlog (·)', '## Velocity', 'Active leases: 1 · open gates: 1',
                    '## Risks', '- flaky CI', '- open gate (question): Ship it?'):
        assert section in status, section
    plan = (tmp_path / 'PLAN.md').read_text()
    assert '## Green tests — backlog (objective, normal)' in plan and '### Wire endpoint — backlog (task, normal, @Builder)' in plan
    assert '- · cargo test passes _(command; 0 evidence)_' in plan
    assert (tmp_path / 'projection.json').exists() and (tmp_path / 'exports' / 'current.bundle.json').exists()
    bundle = json.loads((tmp_path / 'exports' / 'current.bundle.json').read_text())
    assert bundle['extensions']['acheron']['gates'][0]['question'] == 'Ship it?'
