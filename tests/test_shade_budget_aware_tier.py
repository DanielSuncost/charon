"""Budget-aware model tier choice: _run_shade ranks the fast/strong tiers via
the real multi-objective router (routing/policy.py), using real per-model
pricing, once a tree's budget runs low — replacing a plain binary downgrade
with an actual quality/cost tradeoff (see model_registry.route_shade_model).
This is opt-in and defensive: with no tiers configured (the common default),
or under non-metered billing, it falls through to the plain requested
task_complexity unchanged, same as before this existed."""
import json

from charon.tools import ToolContext
from charon.tools.shade_tool import _run_shade
from charon.agents.topology_budget import mint_budget, record_usage
from charon.shade.shade_orchestrator import create_contract


def _setup(tmp_path, monkeypatch, *, token_budget, tokens_used, configure_tiers=False):
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
    if configure_tiers:
        # Real, distinct tier models + no onboarding.json present -> billing
        # resolves to 'metered' by default (resolve_billing_mode's fallback),
        # so route_shade_model actually activates instead of passing through.
        (state_dir / 'model_registry.json').write_text(json.dumps({
            'tiers': {'fast': 'claude-haiku', 'strong': 'claude-opus'},
        }))
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
    """With real tiers configured, a 'complex' request gets routed to the
    cheap tier once the tree's budget is mostly spent — ECONOMY policy,
    no quality floor, so cost dominates regardless of what was asked."""
    state_dir, contract, budget, ctx, captured = _setup(
        tmp_path, monkeypatch, token_budget=1000, tokens_used=800, configure_tiers=True,
    )

    _run_shade(state_dir, 'AG-SHADE', contract['id'], 'test goal', [], [], ctx, 1, budget,
               False, False, 'complex')

    assert captured['task_complexity'] == 'normal'


def test_complex_request_honored_when_budget_has_room(tmp_path, monkeypatch):
    """With real tiers configured and budget healthy, BALANCED policy plus a
    quality floor actually honors a 'complex' request (routes to strong)."""
    state_dir, contract, budget, ctx, captured = _setup(
        tmp_path, monkeypatch, token_budget=1000, tokens_used=100, configure_tiers=True,
    )

    _run_shade(state_dir, 'AG-SHADE', contract['id'], 'test goal', [], [], ctx, 1, budget,
               False, False, 'complex')

    assert captured['task_complexity'] == 'complex'


def test_complex_request_honored_when_budget_unlimited(tmp_path, monkeypatch):
    state_dir, contract, budget, ctx, captured = _setup(
        tmp_path, monkeypatch, token_budget=0, tokens_used=0, configure_tiers=True,
    )

    _run_shade(state_dir, 'AG-SHADE', contract['id'], 'test goal', [], [], ctx, 1, budget,
               False, False, 'complex')

    assert captured['task_complexity'] == 'complex'


def test_non_complex_request_unaffected_by_low_budget(tmp_path, monkeypatch):
    state_dir, contract, budget, ctx, captured = _setup(
        tmp_path, monkeypatch, token_budget=1000, tokens_used=999, configure_tiers=True,
    )

    _run_shade(state_dir, 'AG-SHADE', contract['id'], 'test goal', [], [], ctx, 1, budget,
               False, False, 'normal')

    assert captured['task_complexity'] == 'normal'


def test_no_tiers_configured_passes_task_complexity_through_unchanged(tmp_path, monkeypatch):
    """The common default setup (no tiers.fast/tiers.strong configured)
    must be a total no-op — zero regression from before route_shade_model
    existed, even under severe budget pressure."""
    state_dir, contract, budget, ctx, captured = _setup(
        tmp_path, monkeypatch, token_budget=1000, tokens_used=999, configure_tiers=False,
    )

    _run_shade(state_dir, 'AG-SHADE', contract['id'], 'test goal', [], [], ctx, 1, budget,
               False, False, 'complex')

    assert captured['task_complexity'] == 'complex'
