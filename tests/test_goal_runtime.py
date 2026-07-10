import json


from charon.agents import goal_runtime


def test_ingest_intent_and_context_packet(tmp_path):
    state = tmp_path / 'state'
    meta = goal_runtime.ingest_user_intent(
        state,
        agent_id='AG-1',
        project='/tmp/project-a',
        session_id='sess-a',
        conversation_id='conv-a',
        text='Implement login flow',
    )

    goal = meta['goal']
    assert goal['goal_id'].startswith('goal-')

    goal_runtime.attach_task(
        state,
        project_id=meta['project_id'],
        session_id=meta['session_id'],
        goal_id=goal['goal_id'],
        task_id='task-1',
    )
    goal_runtime.record_result(
        state,
        project_id=meta['project_id'],
        session_id=meta['session_id'],
        goal_id=goal['goal_id'],
        summary='done',
        status='completed',
    )

    packet = goal_runtime.build_context_packet(
        state,
        agent_id='AG-1',
        project_id=meta['project_id'],
        session_id=meta['session_id'],
    )
    assert packet['agent_id'] == 'AG-1'
    assert packet['goal_count_session'] >= 1

    saved = json.loads((state / 'context_packets' / 'AG-1.json').read_text())
    assert saved['agent_id'] == 'AG-1'
