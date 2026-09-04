"""Tests for topology_budget: depth/breadth/total-agent/time governance."""
import time

from charon.agents.topology_budget import (
    PRESETS, effective_budget, mint_budget, try_reserve, current_state,
)


class _FakeCtx:
    def __init__(self, agent_id='', topology_depth=0, topology_budget=None):
        self.agent_id = agent_id
        self.topology_depth = topology_depth
        self.topology_budget = topology_budget


def test_mint_budget_uses_preset_defaults():
    budget = mint_budget('root-1', preset='narrow')
    assert budget['root_id'] == 'root-1'
    assert budget['max_depth'] == PRESETS['narrow']['max_depth']
    assert budget['max_breadth_per_level'] == PRESETS['narrow']['max_breadth_per_level']
    assert budget['max_total_agents'] == PRESETS['narrow']['max_total_agents']


def test_mint_budget_unknown_preset_falls_back_to_standard():
    budget = mint_budget('root-1', preset='nonsense')
    assert budget['max_depth'] == PRESETS['standard']['max_depth']


def test_mint_budget_overrides_win_over_preset():
    budget = mint_budget('root-1', preset='standard', max_depth=9)
    assert budget['max_depth'] == 9
    assert budget['max_breadth_per_level'] == PRESETS['standard']['max_breadth_per_level']


def test_effective_budget_mints_fresh_when_ctx_has_none():
    ctx = _FakeCtx(agent_id='AG-ROOT')
    budget = effective_budget(ctx, preset='wide')
    assert budget['root_id'] == 'AG-ROOT'
    assert budget['max_depth'] == PRESETS['wide']['max_depth']


def test_effective_budget_inherits_and_ignores_descendant_preset():
    """A descendant can't escalate its own limits by requesting a wider preset —
    it always gets back exactly the budget it was handed."""
    inherited = mint_budget('root-1', preset='narrow')
    ctx = _FakeCtx(agent_id='AG-CHILD', topology_budget=inherited)
    budget = effective_budget(ctx, preset='wide')
    assert budget == inherited
    assert budget['max_depth'] == PRESETS['narrow']['max_depth']


def test_try_reserve_allows_within_limits(tmp_path):
    budget = mint_budget('root-1', preset='standard')
    ok, reason = try_reserve(tmp_path, budget, parent_agent_id='AG-1', depth=1)
    assert ok
    assert reason == ''
    state = current_state(tmp_path, 'root-1')
    assert state['total_agents'] == 1
    assert state['children_by_node']['AG-1'] == 1


def test_try_reserve_rejects_beyond_max_depth(tmp_path):
    budget = mint_budget('root-1', max_depth=2, max_breadth_per_level=0, max_total_agents=0)
    ok, reason = try_reserve(tmp_path, budget, parent_agent_id='AG-1', depth=3)
    assert not ok
    assert 'depth' in reason


def test_try_reserve_rejects_beyond_max_breadth(tmp_path):
    budget = mint_budget('root-1', max_depth=0, max_breadth_per_level=2, max_total_agents=0)
    ok1, _ = try_reserve(tmp_path, budget, parent_agent_id='AG-1', depth=1)
    ok2, _ = try_reserve(tmp_path, budget, parent_agent_id='AG-1', depth=1)
    ok3, reason3 = try_reserve(tmp_path, budget, parent_agent_id='AG-1', depth=1)
    assert ok1 and ok2
    assert not ok3
    assert 'breadth' in reason3


def test_try_reserve_breadth_is_per_parent_node(tmp_path):
    """The breadth cap governs children of one node, not the whole tree."""
    budget = mint_budget('root-1', max_depth=0, max_breadth_per_level=1, max_total_agents=0)
    ok_a, _ = try_reserve(tmp_path, budget, parent_agent_id='AG-A', depth=1)
    ok_b, _ = try_reserve(tmp_path, budget, parent_agent_id='AG-B', depth=1)
    assert ok_a and ok_b


def test_try_reserve_rejects_beyond_max_total_agents(tmp_path):
    budget = mint_budget('root-1', max_depth=0, max_breadth_per_level=0, max_total_agents=1)
    ok1, _ = try_reserve(tmp_path, budget, parent_agent_id='AG-1', depth=1)
    ok2, reason2 = try_reserve(tmp_path, budget, parent_agent_id='AG-2', depth=1)
    assert ok1
    assert not ok2
    assert 'total agents' in reason2


def test_try_reserve_respects_count_argument(tmp_path):
    budget = mint_budget('root-1', max_depth=0, max_breadth_per_level=0, max_total_agents=5)
    ok, reason = try_reserve(tmp_path, budget, parent_agent_id='AG-1', depth=1, count=10)
    assert not ok
    assert 'total agents' in reason
    # Rejected reservations must not partially commit.
    state = current_state(tmp_path, 'root-1')
    assert state['total_agents'] == 0


def test_try_reserve_rejects_when_time_budget_exhausted(tmp_path):
    budget = mint_budget('root-1', time_budget_minutes=1)
    budget['started_at'] = time.time() - 120  # 2 minutes ago
    ok, reason = try_reserve(tmp_path, budget, parent_agent_id='AG-1', depth=1)
    assert not ok
    assert 'time budget' in reason


def test_try_reserve_fails_open_on_unwritable_state_dir(tmp_path):
    """Governance is a soft governor: if accounting can't be persisted, the
    spawn is still allowed rather than blocking legitimate work."""
    bogus_state_dir = tmp_path / 'not_a_dir'
    bogus_state_dir.write_text('a file, not a directory')
    budget = mint_budget('root-1', preset='standard')
    ok, reason = try_reserve(bogus_state_dir, budget, parent_agent_id='AG-1', depth=1)
    assert ok
    assert reason == ''
