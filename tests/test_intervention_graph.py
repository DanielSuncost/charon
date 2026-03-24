from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'apps' / 'core-daemon' / 'intervention_graph.py'

spec = importlib.util.spec_from_file_location('intervention_graph', MODULE_PATH)
intervention_graph = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = intervention_graph
spec.loader.exec_module(intervention_graph)


def test_append_message_records_parent_chain(tmp_path):
    log_path = tmp_path / 'interventions.jsonl'

    root_msg = intervention_graph.append_message(
        log_path,
        conversation_id='conv-1',
        actor_agent_id='AG-0001',
        content='Initial plan',
    )
    child_msg = intervention_graph.append_message(
        log_path,
        conversation_id='conv-1',
        actor_agent_id='AG-0001',
        content='Follow-up detail',
        parent_message_id=root_msg['message_id'],
    )

    events = intervention_graph.load_events(log_path)
    assert len(events) == 2
    assert events[0]['parent_message_id'] is None
    assert events[1]['parent_message_id'] == root_msg['message_id']
    assert events[1]['causation_id'] == root_msg['message_id']
    assert child_msg['event_type'] == 'agent_message'


def test_append_intervention_tracks_target_message(tmp_path):
    log_path = tmp_path / 'interventions.jsonl'

    base = intervention_graph.append_message(
        log_path,
        conversation_id='conv-2',
        actor_agent_id='AG-0001',
        content='I will implement parser.',
    )

    intervention = intervention_graph.append_intervention(
        log_path,
        conversation_id='conv-2',
        actor_agent_id='AG-0002',
        content='Intervening: parser should be schema-first.',
        intervention_of_message_id=base['message_id'],
        parent_message_id=base['message_id'],
    )

    assert intervention['event_type'] == 'agent_intervention'
    assert intervention['intervention_of_message_id'] == base['message_id']
    assert intervention['causation_id'] == base['message_id']


def test_reconstruct_path_supports_backtracking(tmp_path):
    log_path = tmp_path / 'interventions.jsonl'

    m1 = intervention_graph.append_message(log_path, conversation_id='conv-3', actor_agent_id='AG-1', content='root')
    m2 = intervention_graph.append_message(log_path, conversation_id='conv-3', actor_agent_id='AG-1', content='branch point', parent_message_id=m1['message_id'])
    m3 = intervention_graph.append_message(log_path, conversation_id='conv-3', actor_agent_id='AG-2', content='alternative direction', parent_message_id=m2['message_id'])

    path = intervention_graph.reconstruct_path(
        log_path,
        conversation_id='conv-3',
        message_id=m3['message_id'],
    )

    assert [node['message_id'] for node in path] == [m1['message_id'], m2['message_id'], m3['message_id']]
