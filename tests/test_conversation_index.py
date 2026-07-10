

from charon.agents import intervention_graph

from charon.conversation import conversation_index


def test_rebuild_index_tracks_conversation_and_interventions(tmp_path):
    log_path = tmp_path / 'interventions.jsonl'

    m1 = intervention_graph.append_message(
        log_path,
        conversation_id='conv-1',
        actor_agent_id='AG-1',
        content='root',
    )
    m2 = intervention_graph.append_message(
        log_path,
        conversation_id='conv-1',
        actor_agent_id='AG-1',
        content='branch',
        parent_message_id=m1['message_id'],
    )
    m3 = intervention_graph.append_intervention(
        log_path,
        conversation_id='conv-1',
        actor_agent_id='AG-2',
        content='intervene',
        intervention_of_message_id=m2['message_id'],
    )

    index = conversation_index.rebuild_index(log_path)

    conv = index['conversations']['conv-1']
    assert conv['message_count'] == 3
    assert conv['last_message_id'] == m3['message_id']
    assert set(conv['agents']) == {'AG-1', 'AG-2'}

    assert m2['message_id'] in index['children'][m1['message_id']]
    assert m3['message_id'] in index['interventions_by_target'][m2['message_id']]


def test_get_path_from_index_returns_root_to_leaf(tmp_path):
    log_path = tmp_path / 'interventions.jsonl'

    m1 = intervention_graph.append_message(log_path, conversation_id='conv-2', actor_agent_id='AG-1', content='root')
    m2 = intervention_graph.append_message(log_path, conversation_id='conv-2', actor_agent_id='AG-1', content='middle', parent_message_id=m1['message_id'])
    m3 = intervention_graph.append_message(log_path, conversation_id='conv-2', actor_agent_id='AG-2', content='leaf', parent_message_id=m2['message_id'])

    index = conversation_index.rebuild_index(log_path)
    path = conversation_index.get_path(index, conversation_id='conv-2', message_id=m3['message_id'])

    assert path == [m1['message_id'], m2['message_id'], m3['message_id']]
