from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'apps' / 'core-daemon' / 'agent_lifecycle.py'

spec = importlib.util.spec_from_file_location('agent_lifecycle', MODULE_PATH)
agent_lifecycle = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = agent_lifecycle
spec.loader.exec_module(agent_lifecycle)


def test_post_agent_message_and_backtrack(tmp_path):
    agent_lifecycle.STATE_DIR = tmp_path / 'state'
    agent_lifecycle.AGENTS_FILE = agent_lifecycle.STATE_DIR / 'agents.json'
    agent_lifecycle.INTERVENTIONS_FILE = agent_lifecycle.STATE_DIR / 'interventions.jsonl'

    m1 = agent_lifecycle.post_agent_message('AG-0001', 'conv-x', 'Root message')
    m2 = agent_lifecycle.post_agent_message('AG-0001', 'conv-x', 'Second message', parent_message_id=m1['message_id'])

    path_nodes = agent_lifecycle.backtrack('conv-x', m2['message_id'])
    assert [n['message_id'] for n in path_nodes] == [m1['message_id'], m2['message_id']]


def test_intervene_records_target_reference(tmp_path):
    agent_lifecycle.STATE_DIR = tmp_path / 'state'
    agent_lifecycle.AGENTS_FILE = agent_lifecycle.STATE_DIR / 'agents.json'
    agent_lifecycle.INTERVENTIONS_FILE = agent_lifecycle.STATE_DIR / 'interventions.jsonl'

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
