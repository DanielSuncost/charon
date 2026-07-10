

from charon.agents import agent_lifecycle


def test_post_agent_message_and_backtrack(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_lifecycle, 'STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr(agent_lifecycle, 'AGENTS_FILE', tmp_path / 'state' / 'agents.json')
    monkeypatch.setattr(agent_lifecycle, 'INTERVENTIONS_FILE', tmp_path / 'state' / 'interventions.jsonl')

    m1 = agent_lifecycle.post_agent_message('AG-0001', 'conv-x', 'Root message')
    m2 = agent_lifecycle.post_agent_message('AG-0001', 'conv-x', 'Second message', parent_message_id=m1['message_id'])

    path_nodes = agent_lifecycle.backtrack('conv-x', m2['message_id'])
    assert [n['message_id'] for n in path_nodes] == [m1['message_id'], m2['message_id']]


def test_intervene_records_target_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_lifecycle, 'STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr(agent_lifecycle, 'AGENTS_FILE', tmp_path / 'state' / 'agents.json')
    monkeypatch.setattr(agent_lifecycle, 'INTERVENTIONS_FILE', tmp_path / 'state' / 'interventions.jsonl')

    base = agent_lifecycle.post_agent_message('AG-0001', 'conv-y', 'Base task')
    event = agent_lifecycle.intervene(
        'AG-0002',
        'conv-y',
        'Pause: add schema validation.',
        intervention_of_message_id=base['message_id'],
    )

    assert event['event_type'] == 'agent_intervention'
    assert event['intervention_of_message_id'] == base['message_id']
    assert event['parent_message_id'] == base['message_id']
