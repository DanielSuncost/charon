

from charon.agents import agent_policy


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
