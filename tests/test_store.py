"""Tests for the Charon SQLite persistence layer (libs/store.py)."""
import json

from libs.store import (
    open_db, DB,
    # agents
    agent_insert, agent_get, agent_list, agent_update, agent_count,
    # tasks
    task_insert, task_get, task_list, task_update, task_delete,
    task_pending, task_all, task_queue_stats,
    # events
    event_append, event_list, event_get_by_message,
    conversation_list, reconstruct_path,
    # shade contracts
    contract_insert, contract_get, contract_list, contract_update,
    # shade phase events
    shade_event_append, shade_event_list,
    # boundaries
    boundary_insert, boundary_get, boundary_list, boundary_update,
    boundary_pending_for_agent,
    # agent runtime
    agent_profile_upsert, agent_profile_get,
    agent_memory_upsert, agent_memory_get,
    agent_inbox_append, agent_inbox_list,
    agent_attempt_append,
    # goals
    goal_project_upsert, goal_project_get,
    goal_session_upsert, goal_session_get,
    goal_context_packet_upsert, goal_context_packet_get,
    # user model
    user_model_get, user_model_set,
    # onboarding
    onboarding_get, onboarding_set,
    # run log
    run_log_append, run_log_tail,
    # migration
    migrate_from_json,
)


def _db(tmp_path) -> DB:
    return open_db(tmp_path / 'state')


# ---------------------------------------------------------------
# Database lifecycle
# ---------------------------------------------------------------

class TestDBLifecycle:
    def test_open_creates_db_file(self, tmp_path):
        db = _db(tmp_path)
        assert db.path.exists()
        db.close()

    def test_open_idempotent(self, tmp_path):
        db1 = open_db(tmp_path / 'state')
        db1.close()
        db2 = open_db(tmp_path / 'state')
        row = db2.fetchone("SELECT value FROM meta WHERE key = 'schema_version'")
        assert row['value'] == '1'
        db2.close()

    def test_wal_mode_enabled(self, tmp_path):
        db = _db(tmp_path)
        row = db.fetchone("PRAGMA journal_mode")
        assert row[list(row.keys())[0]].lower() == 'wal'
        db.close()


# ---------------------------------------------------------------
# Agents
# ---------------------------------------------------------------

class TestAgents:
    def test_insert_and_get(self, tmp_path):
        db = _db(tmp_path)
        a = agent_insert(db, {'id': 'AG-0001', 'name': 'alpha', 'mode': 'persistent', 'goal': 'test'})
        assert a['id'] == 'AG-0001'
        fetched = agent_get(db, 'AG-0001')
        assert fetched is not None
        assert fetched['name'] == 'alpha'
        assert fetched['mode'] == 'persistent'
        db.close()

    def test_list(self, tmp_path):
        db = _db(tmp_path)
        agent_insert(db, {'id': 'AG-0001', 'name': 'a1'})
        agent_insert(db, {'id': 'AG-0002', 'name': 'a2'})
        agents = agent_list(db)
        assert len(agents) == 2
        assert agents[0]['id'] == 'AG-0001'
        db.close()

    def test_update(self, tmp_path):
        db = _db(tmp_path)
        agent_insert(db, {'id': 'AG-0001', 'name': 'old', 'status': 'running'})
        updated = agent_update(db, 'AG-0001', status='stopped', name='new')
        assert updated['status'] == 'stopped'
        assert updated['name'] == 'new'
        db.close()

    def test_update_nonexistent_returns_none(self, tmp_path):
        db = _db(tmp_path)
        assert agent_update(db, 'NOPE', status='stopped') is None
        db.close()

    def test_count(self, tmp_path):
        db = _db(tmp_path)
        assert agent_count(db) == 0
        agent_insert(db, {'id': 'AG-0001', 'name': 'x'})
        assert agent_count(db) == 1
        db.close()

    def test_extra_fields_preserved(self, tmp_path):
        db = _db(tmp_path)
        agent_insert(db, {'id': 'AG-0001', 'name': 'x', 'custom_field': 'hello'})
        fetched = agent_get(db, 'AG-0001')
        assert fetched['custom_field'] == 'hello'
        db.close()

    def test_get_nonexistent_returns_none(self, tmp_path):
        db = _db(tmp_path)
        assert agent_get(db, 'NOPE') is None
        db.close()


# ---------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------

class TestTasks:
    def _sample(self, **overrides):
        t = {
            'id': 'task-001', 'title': 'Test task', 'instruction': 'do things',
            'status': 'pending', 'task_type': 'agent_task',
            'owner_agent_id': 'AG-0001', 'priority': 'normal',
        }
        t.update(overrides)
        return t

    def test_insert_and_get(self, tmp_path):
        db = _db(tmp_path)
        task_insert(db, self._sample())
        fetched = task_get(db, 'task-001')
        assert fetched is not None
        assert fetched['title'] == 'Test task'
        assert fetched['status'] == 'pending'
        db.close()

    def test_pending(self, tmp_path):
        db = _db(tmp_path)
        task_insert(db, self._sample(id='t1', status='pending'))
        task_insert(db, self._sample(id='t2', status='completed'))
        task_insert(db, self._sample(id='t3', status='pending'))
        pending = task_pending(db)
        assert len(pending) == 2
        assert all(t['status'] == 'pending' for t in pending)
        db.close()

    def test_update(self, tmp_path):
        db = _db(tmp_path)
        task_insert(db, self._sample())
        task_update(db, 'task-001', status='completed', result_summary='done')
        fetched = task_get(db, 'task-001')
        assert fetched['status'] == 'completed'
        assert fetched['result_summary'] == 'done'
        db.close()

    def test_delete(self, tmp_path):
        db = _db(tmp_path)
        task_insert(db, self._sample())
        task_delete(db, 'task-001')
        assert task_get(db, 'task-001') is None
        db.close()

    def test_queue_stats(self, tmp_path):
        db = _db(tmp_path)
        task_insert(db, self._sample(id='t1', status='pending'))
        task_insert(db, self._sample(id='t2', status='pending'))
        task_insert(db, self._sample(id='t3', status='in_progress'))
        task_insert(db, self._sample(id='t4', status='completed'))
        task_insert(db, self._sample(id='t5', status='failed'))
        stats = task_queue_stats(db)
        assert stats['pending'] == 2
        assert stats['in_progress'] == 1
        assert stats['completed'] == 1
        assert stats['failed'] == 1
        assert stats['total'] == 5
        db.close()

    def test_list_with_filters(self, tmp_path):
        db = _db(tmp_path)
        task_insert(db, self._sample(id='t1', task_type='agent_task', owner_agent_id='AG-1'))
        task_insert(db, self._sample(id='t2', task_type='user_intent', owner_agent_id='AG-2'))
        task_insert(db, self._sample(id='t3', task_type='agent_task', owner_agent_id='AG-1'))
        result = task_list(db, task_type='agent_task', owner_agent_id='AG-1')
        assert len(result) == 2
        result2 = task_list(db, task_type='user_intent')
        assert len(result2) == 1
        db.close()

    def test_extra_fields_preserved(self, tmp_path):
        db = _db(tmp_path)
        task_insert(db, self._sample(
            scope=['src/api', 'src/models'],
            boundary={'status': 'clear'},
            shade_phase={'contract_id': 'ctr-123'},
        ))
        fetched = task_get(db, 'task-001')
        assert fetched['scope'] == ['src/api', 'src/models']
        assert fetched['boundary'] == {'status': 'clear'}
        assert fetched['shade_phase']['contract_id'] == 'ctr-123'
        db.close()

    def test_task_all(self, tmp_path):
        db = _db(tmp_path)
        task_insert(db, self._sample(id='t1'))
        task_insert(db, self._sample(id='t2'))
        assert len(task_all(db)) == 2
        db.close()


# ---------------------------------------------------------------
# Events
# ---------------------------------------------------------------

class TestEvents:
    def test_append_and_list(self, tmp_path):
        db = _db(tmp_path)
        event_append(db, {
            'event_type': 'agent_message',
            'conversation_id': 'conv-1',
            'message_id': 'msg-001',
            'actor_id': 'AG-1',
            'payload': {'content': 'hello'},
        })
        events = event_list(db, conversation_id='conv-1')
        assert len(events) == 1
        assert events[0]['payload'] == {'content': 'hello'}
        db.close()

    def test_get_by_message(self, tmp_path):
        db = _db(tmp_path)
        event_append(db, {
            'event_type': 'agent_message',
            'message_id': 'msg-abc',
            'payload': {'content': 'test'},
        })
        evt = event_get_by_message(db, 'msg-abc')
        assert evt is not None
        assert evt['message_id'] == 'msg-abc'
        assert event_get_by_message(db, 'nope') is None
        db.close()

    def test_conversation_list(self, tmp_path):
        db = _db(tmp_path)
        event_append(db, {'event_type': 'm', 'conversation_id': 'c1', 'message_id': 'm1', 'actor_id': 'AG-1'})
        event_append(db, {'event_type': 'm', 'conversation_id': 'c1', 'message_id': 'm2', 'actor_id': 'AG-1'})
        event_append(db, {'event_type': 'm', 'conversation_id': 'c2', 'message_id': 'm3', 'actor_id': 'AG-2'})
        convos = conversation_list(db)
        assert len(convos) == 2
        c1 = [c for c in convos if c['conversation_id'] == 'c1'][0]
        assert c1['message_count'] == 2
        db.close()

    def test_reconstruct_path(self, tmp_path):
        db = _db(tmp_path)
        event_append(db, {'event_type': 'm', 'conversation_id': 'c1', 'message_id': 'root', 'actor_id': 'A'})
        event_append(db, {'event_type': 'm', 'conversation_id': 'c1', 'message_id': 'child', 'parent_message_id': 'root', 'actor_id': 'A'})
        event_append(db, {'event_type': 'm', 'conversation_id': 'c1', 'message_id': 'grandchild', 'parent_message_id': 'child', 'actor_id': 'A'})
        path = reconstruct_path(db, conversation_id='c1', message_id='grandchild')
        assert len(path) == 3
        assert path[0]['message_id'] == 'root'
        assert path[-1]['message_id'] == 'grandchild'
        db.close()

    def test_reconstruct_path_empty(self, tmp_path):
        db = _db(tmp_path)
        assert reconstruct_path(db, conversation_id='c1', message_id='nope') == []
        db.close()


# ---------------------------------------------------------------
# Shade contracts
# ---------------------------------------------------------------

class TestShadeContracts:
    def _sample_contract(self, **overrides):
        c = {
            'id': 'ctr-001',
            'status': 'running',
            'parent_task_id': 'task-1',
            'parent_agent_id': 'AG-1',
            'shade_agent_id': 'AG-2',
            'conversation_id': 'conv-1',
            'project': '/tmp/proj',
            'goal': 'Build X',
            'constraints': ['no migrations'],
            'expected_outputs': ['tests pass'],
            'scope': ['src/api'],
            'phases': [
                {'phase_id': 'P01', 'name': 'analysis', 'status': 'pending'},
                {'phase_id': 'P02', 'name': 'impl', 'status': 'pending'},
            ],
            'phase_count': 2,
            'current_phase_id': 'P01',
        }
        c.update(overrides)
        return c

    def test_insert_and_get(self, tmp_path):
        db = _db(tmp_path)
        contract_insert(db, self._sample_contract())
        fetched = contract_get(db, 'ctr-001')
        assert fetched is not None
        assert fetched['goal'] == 'Build X'
        assert len(fetched['phases']) == 2
        assert fetched['constraints'] == ['no migrations']
        db.close()

    def test_list(self, tmp_path):
        db = _db(tmp_path)
        contract_insert(db, self._sample_contract(id='c1', status='running'))
        contract_insert(db, self._sample_contract(id='c2', status='completed'))
        assert len(contract_list(db)) == 2
        assert len(contract_list(db, status='running')) == 1
        db.close()

    def test_update(self, tmp_path):
        db = _db(tmp_path)
        c = self._sample_contract()
        contract_insert(db, c)
        c['status'] = 'completed'
        c['phases'][0]['status'] = 'completed'
        contract_update(db, c)
        fetched = contract_get(db, 'ctr-001')
        assert fetched['status'] == 'completed'
        assert fetched['phases'][0]['status'] == 'completed'
        db.close()


# ---------------------------------------------------------------
# Shade phase events
# ---------------------------------------------------------------

class TestShadePhaseEvents:
    def test_append_and_list(self, tmp_path):
        db = _db(tmp_path)
        shade_event_append(db, contract_id='ctr-1', phase_id='P01', event_type='phase_queued', payload={'task_id': 't1'})
        shade_event_append(db, contract_id='ctr-1', phase_id='P01', event_type='phase_completed', payload={'summary': 'done'})
        shade_event_append(db, contract_id='ctr-2', phase_id='P01', event_type='phase_queued')
        events = shade_event_list(db, 'ctr-1')
        assert len(events) == 2
        assert events[0]['event_type'] == 'phase_queued'
        assert events[1]['payload'] == {'summary': 'done'}
        assert len(shade_event_list(db)) == 3
        db.close()


# ---------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------

class TestBoundaries:
    def test_insert_get_list(self, tmp_path):
        db = _db(tmp_path)
        boundary_insert(db, {
            'id': 'bnd-001', 'proposer_agent_id': 'AG-1',
            'target_agent_id': 'AG-2', 'project': 'proj',
            'scope': ['src/'], 'reason': 'overlap',
        })
        fetched = boundary_get(db, 'bnd-001')
        assert fetched is not None
        assert fetched['scope'] == ['src/']
        assert len(boundary_list(db)) == 1
        db.close()

    def test_update(self, tmp_path):
        db = _db(tmp_path)
        boundary_insert(db, {
            'id': 'bnd-001', 'proposer_agent_id': 'AG-1',
            'target_agent_id': 'AG-2',
        })
        boundary_update(db, 'bnd-001', status='accepted', resolved_by='AG-2')
        fetched = boundary_get(db, 'bnd-001')
        assert fetched['status'] == 'accepted'
        assert fetched['resolved_by'] == 'AG-2'
        db.close()

    def test_pending_for_agent(self, tmp_path):
        db = _db(tmp_path)
        boundary_insert(db, {'id': 'b1', 'proposer_agent_id': 'A', 'target_agent_id': 'B', 'status': 'proposed'})
        boundary_insert(db, {'id': 'b2', 'proposer_agent_id': 'A', 'target_agent_id': 'B', 'status': 'accepted'})
        boundary_insert(db, {'id': 'b3', 'proposer_agent_id': 'A', 'target_agent_id': 'C', 'status': 'proposed'})
        assert len(boundary_pending_for_agent(db, 'B')) == 1
        assert len(boundary_pending_for_agent(db, 'C')) == 1
        db.close()


# ---------------------------------------------------------------
# Agent runtime state
# ---------------------------------------------------------------

class TestAgentRuntime:
    def test_profile_upsert_get(self, tmp_path):
        db = _db(tmp_path)
        agent_profile_upsert(db, 'AG-1', {'name': 'alpha', 'mode': 'persistent'})
        doc = agent_profile_get(db, 'AG-1')
        assert doc['name'] == 'alpha'
        # update
        agent_profile_upsert(db, 'AG-1', {'name': 'beta', 'mode': 'persistent'})
        assert agent_profile_get(db, 'AG-1')['name'] == 'beta'
        db.close()

    def test_memory_upsert_get(self, tmp_path):
        db = _db(tmp_path)
        agent_memory_upsert(db, 'AG-1', {'notes': [{'task_id': 't1', 'summary': 'did stuff'}]})
        doc = agent_memory_get(db, 'AG-1')
        assert len(doc['notes']) == 1
        assert agent_memory_get(db, 'NOPE') is None
        db.close()

    def test_inbox(self, tmp_path):
        db = _db(tmp_path)
        agent_inbox_append(db, 'AG-1', 'task_received', {'task_id': 't1'})
        agent_inbox_append(db, 'AG-1', 'task_succeeded', {'task_id': 't1', 'summary': 'ok'})
        agent_inbox_append(db, 'AG-2', 'task_received', {'task_id': 't2'})
        items = agent_inbox_list(db, 'AG-1')
        assert len(items) == 2
        # most recent first
        assert items[0]['event_type'] == 'task_succeeded'
        db.close()

    def test_attempts(self, tmp_path):
        db = _db(tmp_path)
        agent_attempt_append(db, 'AG-1', 't1', 'att-001', 'started', {'info': 'x'})
        agent_attempt_append(db, 'AG-1', 't1', 'att-001', 'completed', {'summary': 'ok'})
        # just verify no error; we don't have a list function yet but data is there
        rows = db.fetchall("SELECT * FROM agent_attempts WHERE agent_id = 'AG-1'")
        assert len(rows) == 2
        db.close()


# ---------------------------------------------------------------
# Goals
# ---------------------------------------------------------------

class TestGoals:
    def test_project_upsert_get(self, tmp_path):
        db = _db(tmp_path)
        goal_project_upsert(db, 'proj-1', {'project_id': 'proj-1', 'goals': []})
        doc = goal_project_get(db, 'proj-1')
        assert doc['project_id'] == 'proj-1'
        assert goal_project_get(db, 'nope') is None
        db.close()

    def test_session_upsert_get(self, tmp_path):
        db = _db(tmp_path)
        goal_session_upsert(db, 'ses-1', 'proj-1', {'session_id': 'ses-1', 'goals': []})
        doc = goal_session_get(db, 'ses-1')
        assert doc['session_id'] == 'ses-1'
        db.close()

    def test_context_packet(self, tmp_path):
        db = _db(tmp_path)
        goal_context_packet_upsert(db, 'AG-1', {'active_goals': ['g1'], 'summary': 'working on X'})
        pkt = goal_context_packet_get(db, 'AG-1')
        assert pkt['summary'] == 'working on X'
        assert goal_context_packet_get(db, 'NOPE') is None
        db.close()


# ---------------------------------------------------------------
# User model
# ---------------------------------------------------------------

class TestUserModel:
    def test_set_and_get(self, tmp_path):
        db = _db(tmp_path)
        user_model_set(db, 'preferences', {'theme': 'vintage'})
        user_model_set(db, 'projects', {'charon': {'last_active': '2026-01-01'}})
        model = user_model_get(db)
        assert model['preferences'] == {'theme': 'vintage'}
        assert 'charon' in model['projects']
        db.close()


# ---------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------

class TestOnboarding:
    def test_default(self, tmp_path):
        db = _db(tmp_path)
        ob = onboarding_get(db)
        assert ob['complete'] is False
        db.close()

    def test_set_and_get(self, tmp_path):
        db = _db(tmp_path)
        onboarding_set(db, {'complete': True, 'step': 'done', 'provider': 'anthropic'})
        ob = onboarding_get(db)
        assert ob['complete'] is True
        assert ob['provider'] == 'anthropic'
        db.close()


# ---------------------------------------------------------------
# Run log
# ---------------------------------------------------------------

class TestRunLog:
    def test_append_and_tail(self, tmp_path):
        db = _db(tmp_path)
        run_log_append(db, 'loop_start', state_dir='/tmp')
        run_log_append(db, 'task_success', task_id='t1')
        run_log_append(db, 'loop_exit')
        tail = run_log_tail(db, 2)
        assert len(tail) == 2
        assert tail[0]['event'] == 'task_success'
        assert tail[1]['event'] == 'loop_exit'
        db.close()


# ---------------------------------------------------------------
# Migration from JSON
# ---------------------------------------------------------------

class TestMigration:
    def test_migrate_agents_and_queue(self, tmp_path):
        state = tmp_path / 'state'
        state.mkdir()
        # Write JSON files
        (state / 'agents.json').write_text(json.dumps([
            {'id': 'AG-0001', 'name': 'alpha', 'mode': 'persistent', 'goal': 'test',
             'status': 'running', 'created_at': '2026-01-01', 'last_active': '2026-01-01'},
        ]))
        (state / 'queue.json').write_text(json.dumps([
            {'id': 'task-1', 'title': 'Do X', 'status': 'pending', 'task_type': 'agent_task',
             'created_at': '2026-01-01', 'updated_at': '2026-01-01'},
        ]))
        (state / 'interventions.jsonl').write_text(json.dumps({
            'id': 'evt-001', 'ts': '2026-01-01', 'event_type': 'agent_message',
            'conversation_id': 'c1', 'message_id': 'msg-001',
            'actor_id': 'AG-0001', 'payload': {'content': 'hello'},
        }) + '\n')
        (state / 'onboarding.json').write_text(json.dumps({
            'complete': True, 'step': 'done', 'provider': 'local',
        }))

        db = open_db(state)
        summary = migrate_from_json(db, state)
        assert summary['agents'] == 1
        assert summary['tasks'] == 1
        assert summary['events'] == 1
        assert summary['onboarding'] == 1

        # Verify data is queryable
        assert agent_get(db, 'AG-0001')['name'] == 'alpha'
        assert task_get(db, 'task-1')['title'] == 'Do X'
        assert len(event_list(db, conversation_id='c1')) == 1
        assert onboarding_get(db)['complete'] is True

        # Idempotent: running again should not duplicate
        migrate_from_json(db, state)
        assert agent_count(db) == 1
        db.close()

    def test_migrate_shade_contracts(self, tmp_path):
        state = tmp_path / 'state'
        state.mkdir()
        (state / 'shade_contracts.json').write_text(json.dumps([{
            'id': 'ctr-001', 'status': 'running', 'goal': 'X',
            'phases': [{'phase_id': 'P01', 'status': 'pending'}],
            'phase_count': 1, 'created_at': '2026-01-01', 'updated_at': '2026-01-01',
            'parent_agent_id': 'AG-1', 'shade_agent_id': 'AG-2',
            'constraints': [], 'expected_outputs': [], 'scope': [],
        }]))
        (state / 'shade_phase_events.jsonl').write_text(
            json.dumps({'contract_id': 'ctr-001', 'phase_id': 'P01', 'event_type': 'phase_queued', 'payload': {}}) + '\n'
        )

        db = open_db(state)
        summary = migrate_from_json(db, state)
        assert summary['contracts'] == 1
        assert summary['shade_phase_events'] == 1
        assert contract_get(db, 'ctr-001')['goal'] == 'X'
        db.close()

    def test_migrate_missing_files_is_safe(self, tmp_path):
        state = tmp_path / 'state'
        state.mkdir()
        db = open_db(state)
        summary = migrate_from_json(db, state)
        assert summary == {}
        db.close()


# ---------------------------------------------------------------
# Concurrency / WAL
# ---------------------------------------------------------------

class TestConcurrency:
    def test_two_connections_no_lock_error(self, tmp_path):
        """Two connections can read/write without SQLITE_BUSY thanks to WAL."""
        state = tmp_path / 'state'
        db1 = open_db(state)
        db2 = open_db(state)
        agent_insert(db1, {'id': 'AG-1', 'name': 'a1'})
        agent_insert(db2, {'id': 'AG-2', 'name': 'a2'})
        assert agent_count(db1) == 2
        assert agent_count(db2) == 2
        db1.close()
        db2.close()


# ---------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------

class TestEdgeCases:
    def test_empty_queue_stats(self, tmp_path):
        db = _db(tmp_path)
        stats = task_queue_stats(db)
        assert stats['total'] == 0
        db.close()

    def test_task_with_none_fields(self, tmp_path):
        db = _db(tmp_path)
        task_insert(db, {
            'id': 't1', 'status': 'pending',
            'owner_agent_id': None, 'conversation_id': None,
            'result_summary': None,
        })
        fetched = task_get(db, 't1')
        assert fetched is not None
        assert fetched['owner_agent_id'] is None
        db.close()

    def test_large_payload_in_event(self, tmp_path):
        db = _db(tmp_path)
        big_content = 'x' * 50000
        event_append(db, {
            'event_type': 'test',
            'message_id': 'msg-big',
            'payload': {'content': big_content},
        })
        evt = event_get_by_message(db, 'msg-big')
        assert len(evt['payload']['content']) == 50000
        db.close()

    def test_unicode_content(self, tmp_path):
        db = _db(tmp_path)
        event_append(db, {
            'event_type': 'test',
            'message_id': 'msg-uni',
            'payload': {'content': '你好世界 🌍 café résumé'},
        })
        evt = event_get_by_message(db, 'msg-uni')
        assert '你好世界' in evt['payload']['content']
        assert '🌍' in evt['payload']['content']
        db.close()
