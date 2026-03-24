from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
MOD_PATH = ROOT / 'apps' / 'core-daemon' / 'agent_policy.py'

spec = importlib.util.spec_from_file_location('agent_policy', MOD_PATH)
agent_policy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = agent_policy
spec.loader.exec_module(agent_policy)


def test_should_delegate_to_shade_for_complex_task():
    task = {
        'instruction': 'x' * 260,
        'scope': ['src/a', 'src/b'],
        'constraints': [],
        'expected_outputs': [],
    }
    agent = {'role': 'charon'}
    assert agent_policy.should_delegate_to_shade(task, agent) is True


def test_plan_user_intent_returns_instruction_and_goal_ref():
    plan = agent_policy.plan_user_intent('Fix API', project='/tmp/p', conversation_id='conv-1', goal_id='goal-1')
    assert plan['instruction'] == 'Fix API'
    assert plan['goal_id'] == 'goal-1'
