"""Retained shades: SpawnShade(retain=True) goes idle instead of stopping,
and execute_reactivate_shade (the engine behind charon.rlm(child_agent_id=))
can call it again — resuming its prior conversation, staying governed by the
same topology budget without double-counting breadth/total-agent."""
import pytest

from charon.tools import ToolContext
from charon.tools import shade_tool
from charon.tools.shade_tool import execute_spawn_shade, execute_reactivate_shade, _run_shade
from charon.agents import shade_lifecycle
from charon.agents.topology_budget import mint_budget, current_state
from charon.orchestration.fsm import TransitionRejected


def _ctx(tmp_path, **kwargs):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    kwargs.setdefault('agent_id', 'AG-ROOT')
    return ToolContext(project_root=tmp_path, state_dir=state, **kwargs)


def _ok_provider(monkeypatch):
    monkeypatch.setattr(
        'charon.providers.worker_provider.ensure_worker_provider_or_request_clarification',
        lambda *a, **k: {'ok': True},
    )


def _fake_engine_class(monkeypatch, tokens=10):
    monkeypatch.setattr(
        'charon.providers.model_registry.get_shade_provider_and_model',
        lambda *a, **k: ('fake-provider', 'fake-model', {}),
    )

    class _FakeEvent:
        def __init__(self, type_, data):
            self.type = type_
            self.data = data

    class _FakeEngine:
        load_from_store_calls = 0
        last_system_prompt = ''

        def __init__(self, **kwargs):
            self.messages = []
            type(self).last_system_prompt = kwargs.get('system_prompt', '')

        def load_from_store(self):
            type(self).load_from_store_calls += 1
            return 0

        @staticmethod
        def _repair_orphaned_tool_calls(messages):
            return messages

        async def submit_and_collect(self, instruction):
            return 'ok', [_FakeEvent('message_end', {'usage': {'total_tokens': tokens}})]

    monkeypatch.setattr('charon.conversation.conversation_engine.ConversationEngine', _FakeEngine)
    return _FakeEngine


# ---------------------------------------------------------------------------
# SpawnShade(retain=True) at the tool boundary
# ---------------------------------------------------------------------------

def test_spawn_shade_retain_initializes_lifecycle(tmp_path, monkeypatch):
    _ok_provider(monkeypatch)
    monkeypatch.setattr(shade_tool, '_run_shade', lambda *a, **k: None)
    ctx = _ctx(tmp_path)

    result = execute_spawn_shade({'goal': 'be retained', 'retain': True}, ctx)
    assert not result.is_error, result.content

    shade_id = result.details['shade_id']
    assert shade_lifecycle.get_state(ctx.state_dir, shade_id) == 'spawned'


def test_spawn_shade_without_retain_has_no_lifecycle(tmp_path, monkeypatch):
    _ok_provider(monkeypatch)
    monkeypatch.setattr(shade_tool, '_run_shade', lambda *a, **k: None)
    ctx = _ctx(tmp_path)

    result = execute_spawn_shade({'goal': 'ephemeral as usual'}, ctx)
    shade_id = result.details['shade_id']
    assert shade_lifecycle.get_state(ctx.state_dir, shade_id) is None


# ---------------------------------------------------------------------------
# System prompt: retained shades are told they're retained, and nudged
# toward Refine when they hit real external friction
# ---------------------------------------------------------------------------

def test_retained_shade_prompt_identifies_as_retained_and_mentions_refine(tmp_path, monkeypatch):
    from charon.shade.shade_orchestrator import create_contract

    fake_engine = _fake_engine_class(monkeypatch)
    monkeypatch.setattr('charon.agents.agent_lifecycle.set_status', lambda a, s: None)
    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    shade_lifecycle.initialize(state_dir, 'AG-SHADE')
    contract = create_contract(
        state_dir, parent_task_id='', parent_agent_id='AG-ROOT', shade_agent_id='AG-SHADE',
        conversation_id='conv-1', project=str(tmp_path), goal='test goal',
    )
    budget = mint_budget('AG-ROOT', preset='standard')
    ctx = ToolContext(project_root=tmp_path, state_dir=state_dir, agent_id='AG-ROOT')

    _run_shade(state_dir, 'AG-SHADE', contract['id'], 'test goal', [], [], ctx, 1, budget, True)

    prompt = fake_engine.last_system_prompt
    assert 'retained worker agent' in prompt
    assert 'ephemeral' not in prompt
    assert 'Refine' in prompt


def test_ephemeral_shade_prompt_says_ephemeral_and_skips_refine_mention(tmp_path, monkeypatch):
    from charon.shade.shade_orchestrator import create_contract

    fake_engine = _fake_engine_class(monkeypatch)
    monkeypatch.setattr('charon.agents.agent_lifecycle.set_status', lambda a, s: None)
    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    contract = create_contract(
        state_dir, parent_task_id='', parent_agent_id='AG-ROOT', shade_agent_id='AG-SHADE',
        conversation_id='conv-1', project=str(tmp_path), goal='test goal',
    )
    budget = mint_budget('AG-ROOT', preset='standard')
    ctx = ToolContext(project_root=tmp_path, state_dir=state_dir, agent_id='AG-ROOT')

    _run_shade(state_dir, 'AG-SHADE', contract['id'], 'test goal', [], [], ctx, 1, budget, False)

    prompt = fake_engine.last_system_prompt
    assert 'ephemeral worker agent' in prompt
    assert 'Refine' not in prompt


# ---------------------------------------------------------------------------
# _run_shade end-of-run behavior
# ---------------------------------------------------------------------------

def test_run_shade_retain_goes_idle_not_stopped(tmp_path, monkeypatch):
    from charon.shade.shade_orchestrator import create_contract

    _fake_engine_class(monkeypatch)
    status_calls = []
    monkeypatch.setattr('charon.agents.agent_lifecycle.set_status', lambda a, s: status_calls.append((a, s)))

    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    shade_lifecycle.initialize(state_dir, 'AG-SHADE')
    contract = create_contract(
        state_dir, parent_task_id='', parent_agent_id='AG-ROOT', shade_agent_id='AG-SHADE',
        conversation_id='conv-1', project=str(tmp_path), goal='test goal',
    )
    budget = mint_budget('AG-ROOT', preset='standard')
    ctx = ToolContext(project_root=tmp_path, state_dir=state_dir, agent_id='AG-ROOT')

    _run_shade(state_dir, 'AG-SHADE', contract['id'], 'test goal', [], [], ctx, 1, budget, True)

    assert shade_lifecycle.get_state(state_dir, 'AG-SHADE') == 'idle'
    assert ('AG-SHADE', 'stopped') not in status_calls


def test_run_shade_without_retain_still_stops(tmp_path, monkeypatch):
    from charon.shade.shade_orchestrator import create_contract

    _fake_engine_class(monkeypatch)
    status_calls = []
    monkeypatch.setattr('charon.agents.agent_lifecycle.set_status', lambda a, s: status_calls.append((a, s)))

    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    contract = create_contract(
        state_dir, parent_task_id='', parent_agent_id='AG-ROOT', shade_agent_id='AG-SHADE',
        conversation_id='conv-1', project=str(tmp_path), goal='test goal',
    )
    budget = mint_budget('AG-ROOT', preset='standard')
    ctx = ToolContext(project_root=tmp_path, state_dir=state_dir, agent_id='AG-ROOT')

    _run_shade(state_dir, 'AG-SHADE', contract['id'], 'test goal', [], [], ctx, 1, budget, False)

    assert shade_lifecycle.get_state(state_dir, 'AG-SHADE') is None
    assert ('AG-SHADE', 'stopped') in status_calls


# ---------------------------------------------------------------------------
# execute_reactivate_shade
# ---------------------------------------------------------------------------

def test_reactivate_shade_reloads_history_and_returns_to_idle(tmp_path, monkeypatch):
    from charon.shade.shade_orchestrator import create_contract, load_contracts

    fake_engine = _fake_engine_class(monkeypatch)
    monkeypatch.setattr('charon.agents.agent_lifecycle.set_status', lambda a, s: None)

    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    shade_lifecycle.initialize(state_dir, 'AG-SHADE')
    contract = create_contract(
        state_dir, parent_task_id='', parent_agent_id='AG-ROOT', shade_agent_id='AG-SHADE',
        conversation_id='conv-1', project=str(tmp_path), goal='first goal',
    )
    budget = mint_budget('AG-ROOT', preset='standard')
    ctx = ToolContext(project_root=tmp_path, state_dir=state_dir, agent_id='AG-ROOT')
    _run_shade(state_dir, 'AG-SHADE', contract['id'], 'first goal', [], [], ctx, 1, budget, True)
    assert shade_lifecycle.get_state(state_dir, 'AG-SHADE') == 'idle'

    handle = execute_reactivate_shade(state_dir, 'AG-SHADE', 'follow-up objective', ctx, depth=1, budget=budget)
    assert handle['status'] == 'running'
    reactivate_contract_id = handle['contract_id']
    assert reactivate_contract_id != contract['id']

    # execute_reactivate_shade launches a background thread; run its target
    # synchronously here for a deterministic assertion instead of polling.
    _run_shade(state_dir, 'AG-SHADE', reactivate_contract_id, 'follow-up objective', [], [], ctx, 1, budget, True, True)

    assert fake_engine.load_from_store_calls >= 1
    assert shade_lifecycle.get_state(state_dir, 'AG-SHADE') == 'idle'
    reactivate_contract = {c['id']: c for c in load_contracts(state_dir)}[reactivate_contract_id]
    assert reactivate_contract['metadata']['reactivation'] is True


def test_reactivate_shade_rejects_unknown_id(tmp_path):
    ctx = _ctx(tmp_path)
    budget = mint_budget('AG-ROOT', preset='standard')
    with pytest.raises(KeyError):
        execute_reactivate_shade(ctx.state_dir, 'AG-NEVER-SPAWNED', 'do it', ctx, depth=1, budget=budget)


def test_reactivate_shade_rejects_non_idle(tmp_path, monkeypatch):
    monkeypatch.setattr('charon.agents.agent_lifecycle.set_status', lambda a, s: None)
    ctx = _ctx(tmp_path)
    shade_lifecycle.initialize(ctx.state_dir, 'AG-SHADE')  # still 'spawned', not idle
    budget = mint_budget('AG-ROOT', preset='standard')
    with pytest.raises(TransitionRejected):
        execute_reactivate_shade(ctx.state_dir, 'AG-SHADE', 'do it', ctx, depth=1, budget=budget)


def test_reactivate_shade_does_not_double_count_breadth_or_total(tmp_path, monkeypatch):
    monkeypatch.setattr('charon.agents.agent_lifecycle.set_status', lambda a, s: None)
    monkeypatch.setattr(shade_tool, '_run_shade', lambda *a, **k: None)  # don't actually run
    ctx = _ctx(tmp_path)
    shade_lifecycle.initialize(ctx.state_dir, 'AG-SHADE')
    shade_lifecycle.mark_idle(ctx.state_dir, 'AG-SHADE')
    budget = mint_budget('root-x', max_depth=0, max_breadth_per_level=1, max_total_agents=1)

    before = current_state(ctx.state_dir, 'root-x')
    handle = execute_reactivate_shade(ctx.state_dir, 'AG-SHADE', 'do it', ctx, depth=1, budget=budget)
    assert handle['status'] == 'running'
    after = current_state(ctx.state_dir, 'root-x')

    assert after['total_agents'] == before['total_agents']
    assert after['children_by_node'] == before['children_by_node']
    assert after['reactivations_by_node']['AG-SHADE'] == 1


def test_reactivate_shade_refused_once_reactivation_cap_hit(tmp_path, monkeypatch):
    monkeypatch.setattr('charon.agents.agent_lifecycle.set_status', lambda a, s: None)
    monkeypatch.setattr(shade_tool, '_run_shade', lambda *a, **k: None)
    ctx = _ctx(tmp_path)
    shade_lifecycle.initialize(ctx.state_dir, 'AG-SHADE')
    budget = mint_budget('root-y', max_depth=0, max_breadth_per_level=0, max_total_agents=0)

    from charon.agents.topology_budget import _state_path
    import json
    # Pre-seed the reactivation counter at the cap so the very next attempt is refused.
    state_path = _state_path(ctx.state_dir, 'root-y')
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        'total_agents': 0, 'children_by_node': {}, 'total_tokens_used': 0,
        'reactivations_by_node': {'AG-SHADE': 20},
    }))
    shade_lifecycle.mark_idle(ctx.state_dir, 'AG-SHADE')

    with pytest.raises(RuntimeError, match='reactivat'):
        execute_reactivate_shade(ctx.state_dir, 'AG-SHADE', 'do it', ctx, depth=1, budget=budget)
    # Refused before the FSM was ever touched — the shade is still idle, not running.
    assert shade_lifecycle.get_state(ctx.state_dir, 'AG-SHADE') == 'idle'
