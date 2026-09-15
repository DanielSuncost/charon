"""A worker's reasoning effort follows task_complexity and degrades with budget,
through the same registry that already picks its model tier."""
import json

import pytest

from charon.agents.topology_budget import mint_budget, record_usage
from charon.providers.model_registry import load_registry, route_shade_effort, save_registry


def test_effort_follows_task_complexity_by_default(tmp_path):
    assert route_shade_effort(tmp_path, task_complexity='simple') == 'low'
    assert route_shade_effort(tmp_path, task_complexity='normal') == 'medium'
    assert route_shade_effort(tmp_path, task_complexity='complex') == 'high'
    assert route_shade_effort(tmp_path, task_complexity='whatever') == 'medium'


def test_registry_table_overrides_the_defaults(tmp_path):
    (tmp_path / 'model_registry.json').write_text(json.dumps({
        'effort_by_complexity': {'complex': 'xhigh', 'simple': 'minimal'},
    }))
    reg = load_registry(tmp_path)
    assert reg['effort_by_complexity'] == {'simple': 'minimal', 'normal': 'medium', 'complex': 'xhigh'}
    assert route_shade_effort(tmp_path, task_complexity='complex') == 'xhigh'
    assert route_shade_effort(tmp_path, task_complexity='simple') == 'minimal'
    assert route_shade_effort(tmp_path, task_complexity='normal') == 'medium'


def test_registry_round_trips_the_effort_settings(tmp_path):
    reg = load_registry(tmp_path)
    reg['shade_effort_mode'] = 'same'
    reg['effort_by_complexity']['complex'] = 'max'
    save_registry(tmp_path, reg)
    loaded = load_registry(tmp_path)
    assert loaded['shade_effort_mode'] == 'same'
    assert loaded['effort_by_complexity']['complex'] == 'max'


def test_effort_steps_down_once_when_budget_is_scarce(tmp_path):
    budget = mint_budget('AG-ROOT', preset='standard', token_budget=1000)
    record_usage(tmp_path, budget, 800)                          # 80% used → scarce
    assert route_shade_effort(tmp_path, task_complexity='complex', budget=budget) == 'medium'
    assert route_shade_effort(tmp_path, task_complexity='normal', budget=budget) == 'low'
    assert route_shade_effort(tmp_path, task_complexity='simple', budget=budget) == 'low'


def test_effort_untouched_while_budget_has_room(tmp_path):
    budget = mint_budget('AG-ROOT', preset='standard', token_budget=1000)
    record_usage(tmp_path, budget, 100)
    assert route_shade_effort(tmp_path, task_complexity='complex', budget=budget) == 'high'


def test_hard_contract_is_not_starved_to_the_floor(tmp_path):
    (tmp_path / 'model_registry.json').write_text(json.dumps({'effort_by_complexity': {'complex': 'ultra'}}))
    budget = mint_budget('AG-ROOT', preset='standard', token_budget=1000)
    record_usage(tmp_path, budget, 990)
    # one notch, not a collapse to 'low'
    assert route_shade_effort(tmp_path, task_complexity='complex', budget=budget) == 'max'


def test_same_mode_inherits_the_session_level(tmp_path, monkeypatch):
    (tmp_path / 'model_registry.json').write_text(json.dumps({'shade_effort_mode': 'same'}))
    (tmp_path / 'onboarding.json').write_text(json.dumps({'thinking_level': 'xhigh'}))
    assert route_shade_effort(tmp_path, task_complexity='simple') == 'xhigh'
    assert route_shade_effort(tmp_path, task_complexity='simple', user_level='low') == 'low'

    # env override wins over the file, like CHARON_SHADE_MODEL_MODE does
    (tmp_path / 'model_registry.json').write_text(json.dumps({'shade_effort_mode': 'auto'}))
    monkeypatch.setenv('CHARON_SHADE_EFFORT_MODE', 'same')
    assert route_shade_effort(tmp_path, task_complexity='simple') == 'xhigh'


def test_effort_routing_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr('charon.providers.model_registry.load_registry',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('registry exploded')))
    assert route_shade_effort(tmp_path, task_complexity='complex') == 'high'
    assert route_shade_effort(tmp_path, task_complexity='simple') == 'low'


# ── through _run_shade: the routed effort reaches the worker's engine ─────────

def _run(tmp_path, monkeypatch, *, task_complexity: str, token_budget: int, tokens_used: int):
    from charon.tools import ToolContext
    from charon.tools.shade_tool import _run_shade
    from charon.shade.shade_orchestrator import create_contract

    captured: dict = {}

    class _FakeEvent:
        def __init__(self, type_, data):
            self.type = type_
            self.data = data

    class _FakeEngine:
        def __init__(self, **kwargs):
            captured['engine_kwargs'] = kwargs
            self.messages = []

        async def submit_and_collect(self, instruction):
            return 'ok', [_FakeEvent('message_end', {'usage': {'total_tokens': 1}})]

    monkeypatch.setattr('charon.providers.model_registry.get_shade_provider_and_model',
                        lambda state_dir, **kwargs: ('fake-provider', 'fake-model', {}))
    monkeypatch.setattr('charon.conversation.conversation_engine.ConversationEngine', _FakeEngine)
    monkeypatch.setattr('charon.agents.agent_lifecycle.set_status', lambda a, s: None)

    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    budget = mint_budget('AG-ROOT', preset='standard', token_budget=token_budget)
    if tokens_used:
        record_usage(state_dir, budget, tokens_used)
    contract = create_contract(
        state_dir, parent_task_id='', parent_agent_id='AG-ROOT', shade_agent_id='AG-SHADE',
        conversation_id='conv-1', project=str(tmp_path), goal='test goal',
    )
    ctx = ToolContext(project_root=tmp_path, state_dir=state_dir, agent_id='AG-ROOT')
    _run_shade(state_dir, 'AG-SHADE', contract['id'], 'test goal', [], [], ctx, 1, budget,
               False, False, task_complexity)
    return captured['engine_kwargs']


@pytest.mark.parametrize('task_complexity,expected', [('simple', 'low'), ('normal', 'medium'), ('complex', 'high')])
def test_shade_engine_gets_the_complexity_effort(tmp_path, monkeypatch, task_complexity, expected):
    kwargs = _run(tmp_path, monkeypatch, task_complexity=task_complexity, token_budget=1000, tokens_used=100)
    assert kwargs['thinking_level'] == expected


def test_shade_engine_effort_degrades_with_budget(tmp_path, monkeypatch):
    kwargs = _run(tmp_path, monkeypatch, task_complexity='complex', token_budget=1000, tokens_used=800)
    assert kwargs['thinking_level'] == 'medium'


def test_shade_engine_effort_falls_back_to_session_default_on_routing_failure(tmp_path, monkeypatch):
    monkeypatch.setattr('charon.providers.model_registry.route_shade_effort',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    kwargs = _run(tmp_path, monkeypatch, task_complexity='complex', token_budget=1000, tokens_used=0)
    assert kwargs['thinking_level'] is None      # engine loads the session level itself
