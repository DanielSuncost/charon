"""Budget-aware routing: _run_shade downgrades a requested 'complex' model
tier back to the cheap tier once a tree has spent most of its token_budget,
using the SAME tier system get_shade_provider_and_model already consults —
not the separate graph/routing policy engine, which classic shades don't use
at all."""
from charon.tools import ToolContext
from charon.tools.shade_tool import _run_shade
from charon.agents.topology_budget import mint_budget, record_usage
from charon.shade.shade_orchestrator import create_contract


def _setup(tmp_path, monkeypatch, *, token_budget, tokens_used):
    captured = {}

    def _fake_get_shade_provider_and_model(state_dir, **kwargs):
        captured['task_complexity'] = kwargs.get('task_complexity')
        return ('fake-provider', 'fake-model', {})

    class _FakeEvent:
        def __init__(self, type_, data):
            self.type = type_
            self.data = data

    class _FakeEngine:
        def __init__(self, **kwargs):
            self.messages = []

        async def submit_and_collect(self, instruction):
            return 'ok', [_FakeEvent('message_end', {'usage': {'total_tokens': 1}})]

    monkeypatch.setattr('charon.providers.model_registry.get_shade_provider_and_model', _fake_get_shade_provider_and_model)
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
    return state_dir, contract, budget, ctx, captured


def test_complex_request_downgraded_once_budget_mostly_spent(tmp_path, monkeypatch):
    state_dir, contract, budget, ctx, captured = _setup(tmp_path, monkeypatch, token_budget=1000, tokens_used=800)

    _run_shade(state_dir, 'AG-SHADE', contract['id'], 'test goal', [], [], ctx, 1, budget,
               False, False, 'complex')

    assert captured['task_complexity'] == 'normal'


def test_complex_request_honored_when_budget_has_room(tmp_path, monkeypatch):
    state_dir, contract, budget, ctx, captured = _setup(tmp_path, monkeypatch, token_budget=1000, tokens_used=100)

    _run_shade(state_dir, 'AG-SHADE', contract['id'], 'test goal', [], [], ctx, 1, budget,
               False, False, 'complex')

    assert captured['task_complexity'] == 'complex'


def test_complex_request_honored_when_budget_unlimited(tmp_path, monkeypatch):
    state_dir, contract, budget, ctx, captured = _setup(tmp_path, monkeypatch, token_budget=0, tokens_used=0)

    _run_shade(state_dir, 'AG-SHADE', contract['id'], 'test goal', [], [], ctx, 1, budget,
               False, False, 'complex')

    assert captured['task_complexity'] == 'complex'


def test_non_complex_request_unaffected_by_low_budget(tmp_path, monkeypatch):
    state_dir, contract, budget, ctx, captured = _setup(tmp_path, monkeypatch, token_budget=1000, tokens_used=999)

    _run_shade(state_dir, 'AG-SHADE', contract['id'], 'test goal', [], [], ctx, 1, budget,
               False, False, 'normal')

    assert captured['task_complexity'] == 'normal'
