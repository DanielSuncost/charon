"""SpawnShade / SpawnBatch must consult topology_budget before spawning, and
must thread depth/budget down to whatever they spawn so a delegation tree
stays governed at every level, not just at its root."""
import re

from charon.tools import ToolContext
from charon.tools.shade_tool import execute_spawn_shade
from charon.tools.batch_tool import execute_spawn_batch
from charon.shade.shade_orchestrator import load_contracts
from charon.agents.topology_budget import current_state


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
    # Don't actually run the background shade engine — these tests only
    # exercise the synchronous spawn-time topology check and bookkeeping.
    monkeypatch.setattr('charon.tools.shade_tool._run_shade', lambda *a, **k: None)


def test_spawn_shade_records_depth_and_budget_on_contract(tmp_path, monkeypatch):
    _ok_provider(monkeypatch)
    ctx = _ctx(tmp_path)

    result = execute_spawn_shade({'goal': 'Do a thing', 'topology_preset': 'narrow'}, ctx)
    assert not result.is_error, result.content

    contracts = {c['id']: c for c in load_contracts(ctx.state_dir)}
    contract = contracts[result.details['contract_id']]
    assert contract['metadata']['topology_depth'] == 1
    assert contract['metadata']['topology_budget']['root_id'] == 'AG-ROOT'
    assert contract['metadata']['topology_budget']['max_depth'] == 1

    state = current_state(ctx.state_dir, 'AG-ROOT')
    assert state['total_agents'] == 1


def test_spawn_shade_refuses_past_max_depth(tmp_path, monkeypatch):
    _ok_provider(monkeypatch)
    inherited_budget = {
        'root_id': 'AG-ROOT', 'max_depth': 1, 'max_breadth_per_level': 50,
        'max_total_agents': 200, 'token_budget': 0, 'time_budget_minutes': 0,
        'started_at': 0,
    }
    # This shade is already at depth 1 of a tree whose ceiling is depth 1 —
    # it must not be able to spawn a depth-2 child.
    ctx = _ctx(tmp_path, topology_depth=1, topology_budget=inherited_budget)

    result = execute_spawn_shade({'goal': 'Delegate further'}, ctx)
    assert result.is_error
    assert 'topology depth' in result.content


def test_spawn_shade_refuses_past_max_breadth_across_calls(tmp_path, monkeypatch):
    """The breadth cap must accumulate across repeated SpawnShade calls from
    the same parent, not just within a single call."""
    _ok_provider(monkeypatch)
    ctx = _ctx(tmp_path)

    r1 = execute_spawn_shade({'goal': 'First', 'topology_preset': 'narrow'}, ctx)
    r2 = execute_spawn_shade({'goal': 'Second', 'topology_preset': 'narrow'}, ctx)
    r3 = execute_spawn_shade({'goal': 'Third', 'topology_preset': 'narrow'}, ctx)
    r4 = execute_spawn_shade({'goal': 'Fourth — over the narrow cap of 3', 'topology_preset': 'narrow'}, ctx)

    assert not r1.is_error and not r2.is_error and not r3.is_error
    assert r4.is_error
    assert 'topology breadth' in r4.content


def test_spawn_shade_descendant_cannot_escalate_inherited_preset(tmp_path, monkeypatch):
    """A shade already governed by a narrow budget can't widen it by passing
    topology_preset='wide' on its own spawn call."""
    _ok_provider(monkeypatch)
    inherited_budget = {
        'root_id': 'AG-ROOT', 'max_depth': 5, 'max_breadth_per_level': 1,
        'max_total_agents': 200, 'token_budget': 0, 'time_budget_minutes': 0,
        'started_at': 0,
    }
    ctx = _ctx(tmp_path, agent_id='AG-CHILD', topology_depth=1, topology_budget=inherited_budget)

    r1 = execute_spawn_shade({'goal': 'First', 'topology_preset': 'wide'}, ctx)
    r2 = execute_spawn_shade({'goal': 'Second — over inherited breadth of 1', 'topology_preset': 'wide'}, ctx)

    assert not r1.is_error
    assert r2.is_error
    assert 'topology breadth' in r2.content


def test_spawn_batch_refuses_when_task_count_exceeds_total_agents(tmp_path, monkeypatch):
    _ok_provider(monkeypatch)
    ctx = _ctx(tmp_path)

    tasks = [{'title': f'T{i}', 'instruction': f'do {i}'} for i in range(4)]
    result = execute_spawn_batch(
        {'goal': 'batch', 'tasks': tasks, 'topology_preset': 'narrow'}, ctx,
    )
    assert result.is_error
    assert 'topology' in result.content


def test_spawn_batch_records_depth_and_budget_on_batch(tmp_path, monkeypatch):
    _ok_provider(monkeypatch)
    from charon.automation import batch_orchestrator
    monkeypatch.setattr(batch_orchestrator, 'run_batch_worker', lambda *a, **k: None)

    ctx = _ctx(tmp_path)
    tasks = [{'title': 'T1', 'instruction': 'do 1'}]
    result = execute_spawn_batch({'goal': 'batch', 'tasks': tasks}, ctx)
    assert not result.is_error, result.content

    bid = re.search(r'batch-[0-9a-f]+', result.content).group(0)
    batch = batch_orchestrator.get_batch(ctx.state_dir, bid)
    assert batch['topology_depth'] == 1
    assert batch['topology_budget']['root_id'] == 'AG-ROOT'
