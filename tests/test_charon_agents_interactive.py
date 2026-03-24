from pathlib import Path
import argparse
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / 'scripts' / 'charon_agents.py'

spec_cli = importlib.util.spec_from_file_location('charon_agents_cli_interactive_test', SCRIPT_PATH)
charon_agents = importlib.util.module_from_spec(spec_cli)
sys.modules[spec_cli.name] = charon_agents
spec_cli.loader.exec_module(charon_agents)


def test_prompt_enqueues_user_intent_task(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    charon_agents.STATE_DIR = state

    agents = [
        {
            'id': 'AG-9001',
            'name': 'charon-main',
            'mode': 'persistent',
            'status': 'running',
            'project': '/tmp/proj',
            'goal': 'help user',
            'role': 'charon',
            'visibility': 'user',
        }
    ]
    (state / 'agents.json').write_text(json.dumps(agents, indent=2))

    args = argparse.Namespace(
        agent_id='AG-9001',
        message='run: echo hi',
        project='',
        session_id='sess-1',
        conversation_id='conv-1',
        wait=False,
        timeout_sec=1.0,
        limit=10,
    )

    out = io.StringIO()
    with redirect_stdout(out):
        charon_agents.cmd_agent_prompt(args)

    queue = json.loads((state / 'queue.json').read_text())
    assert len(queue) == 1
    task = queue[0]
    assert task['task_type'] == 'user_intent'
    assert task['owner_agent_id'] == 'AG-9001'
    assert task['conversation_id'] == 'conv-1'


def test_prompt_fails_fast_for_unknown_agent(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    charon_agents.STATE_DIR = state
    (state / 'agents.json').write_text('[]')

    args = argparse.Namespace(
        agent_id='AG-404',
        message='hello',
        project='',
        session_id='',
        conversation_id='',
        wait=False,
        timeout_sec=1.0,
        limit=10,
    )

    with redirect_stdout(io.StringIO()):
        try:
            charon_agents.cmd_agent_prompt(args)
            assert False, 'expected SystemExit'
        except SystemExit as e:
            assert e.code == 2


def test_thread_prints_intervention_content(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    charon_agents.STATE_DIR = state

    event = {
        'ts': '2026-01-01T00:00:00+00:00',
        'conversation_id': 'conv-xyz',
        'actor_id': 'AG-1',
        'payload': {'content': 'hello from agent'},
    }
    (state / 'interventions.jsonl').write_text(json.dumps(event) + '\n')

    args = argparse.Namespace(conversation_id='conv-xyz', agent_id='', limit=10)
    out = io.StringIO()
    with redirect_stdout(out):
        charon_agents.cmd_agent_thread(args)

    rendered = out.getvalue().strip()
    assert 'conv-xyz' in rendered
    assert 'AG-1' in rendered
    assert 'hello from agent' in rendered
