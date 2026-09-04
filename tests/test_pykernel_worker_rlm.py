"""Tests for _pykernel_worker.rlm() — the blocking recursive-call primitive.

Exercised directly against the bridge-module closure (no real kernel
subprocess needed), with execute_spawn_shade/get_contract mocked — mirrors
how test_tools_pykernel.py checks `callable(charon.spawn_shade)` without a
live provider, just going one level deeper into the actual polling logic.
"""
import json

from charon.tools import ToolResult
from charon.tools import _pykernel_worker


def _mod(tmp_path, agent_id='AG-ROOT'):
    return _pykernel_worker._bridge_module(str(tmp_path), str(tmp_path / 'state'), agent_id)


def test_rlm_blocks_and_returns_completed_output(tmp_path, monkeypatch):
    calls = {'n': 0}

    def _fake_spawn(params, ctx):
        return ToolResult(content='ok', details={'contract_id': 'ctr-1', 'shade_id': 'AG-X', 'status': 'running'})

    def _fake_get_contract(state_dir, contract_id):
        calls['n'] += 1
        status = 'completed' if calls['n'] >= 2 else 'running'
        return {
            'status': status,
            'phases': [{'result_summary': 'the final answer'}],
            'metadata': {'topology_depth': 1, 'topology_budget': {'root_id': 'AG-ROOT'}},
        }

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)
    monkeypatch.setattr('charon.shade.shade_orchestrator.get_contract', _fake_get_contract)

    result = _mod(tmp_path).rlm('do the thing', poll_interval=0)

    assert result == {'status': 'completed', 'output': 'the final answer', 'contract_id': 'ctr-1'}
    assert calls['n'] >= 2  # actually polled more than once before hitting a terminal status


def test_rlm_returns_last_error_as_output_on_failure(tmp_path, monkeypatch):
    def _fake_spawn(params, ctx):
        return ToolResult(content='ok', details={'contract_id': 'ctr-2'})

    def _fake_get_contract(state_dir, contract_id):
        return {'status': 'failed', 'phases': [], 'last_error': 'boom', 'metadata': {}}

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)
    monkeypatch.setattr('charon.shade.shade_orchestrator.get_contract', _fake_get_contract)

    result = _mod(tmp_path).rlm('do the thing', poll_interval=0)
    assert result == {'status': 'failed', 'output': 'boom', 'contract_id': 'ctr-2'}


def test_rlm_raises_when_spawn_fails(tmp_path, monkeypatch):
    def _fake_spawn(params, ctx):
        return ToolResult(content='no provider configured', is_error=True)

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)

    try:
        _mod(tmp_path).rlm('do the thing')
        raise AssertionError('expected RuntimeError')
    except RuntimeError as e:
        assert 'no provider configured' in str(e)


def test_rlm_child_agent_id_reactivates_and_blocks(tmp_path, monkeypatch):
    calls = {'n': 0}

    def _fake_reactivate(state_dir, child_agent_id, objective, ctx, *, depth, budget):
        assert child_agent_id == 'AG-RETAINED'
        assert objective == 'follow-up instruction'
        return {'shade_id': child_agent_id, 'contract_id': 'ctr-reactivated', 'status': 'running'}

    def _fake_get_contract(state_dir, contract_id):
        calls['n'] += 1
        status = 'completed' if calls['n'] >= 2 else 'running'
        return {'status': status, 'phases': [{'result_summary': 'resumed answer'}], 'metadata': {}}

    monkeypatch.setattr('charon.tools.shade_tool.execute_reactivate_shade', _fake_reactivate)
    monkeypatch.setattr('charon.shade.shade_orchestrator.get_contract', _fake_get_contract)

    result = _mod(tmp_path).rlm('follow-up instruction', child_agent_id='AG-RETAINED', poll_interval=0)

    assert result == {'status': 'completed', 'output': 'resumed answer', 'contract_id': 'ctr-reactivated'}


def test_rlm_child_agent_id_never_falls_back_to_spawning(tmp_path, monkeypatch):
    """A reactivation attempt against an id with no retained lifecycle must
    error, never silently spawn a fresh shade under that id."""
    spawn_called = {'n': 0}

    def _fake_spawn(params, ctx):
        spawn_called['n'] += 1
        return ToolResult(content='ok', details={'contract_id': 'should-not-happen'})

    def _fake_reactivate(state_dir, child_agent_id, objective, ctx, *, depth, budget):
        raise KeyError(child_agent_id)

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)
    monkeypatch.setattr('charon.tools.shade_tool.execute_reactivate_shade', _fake_reactivate)

    try:
        _mod(tmp_path).rlm('do the thing', child_agent_id='AG-NEVER-RETAINED')
        raise AssertionError('expected RuntimeError')
    except RuntimeError as e:
        assert 'no retained lifecycle' in str(e)
    assert spawn_called['n'] == 0


def test_rlm_contract_id_reattaches_without_spawning(tmp_path, monkeypatch):
    spawn_called = {'n': 0}

    def _fake_spawn(params, ctx):
        spawn_called['n'] += 1
        return ToolResult(content='ok', details={'contract_id': 'should-not-be-used'})

    def _fake_get_contract(state_dir, contract_id):
        assert contract_id == 'ctr-existing'
        return {'status': 'completed', 'phases': [{'result_summary': 'done'}], 'metadata': {}}

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)
    monkeypatch.setattr('charon.shade.shade_orchestrator.get_contract', _fake_get_contract)

    result = _mod(tmp_path).rlm('irrelevant once resuming', contract_id='ctr-existing', poll_interval=0)

    assert spawn_called['n'] == 0
    assert result['contract_id'] == 'ctr-existing'


def test_rlm_writes_trace_log_on_completion(tmp_path, monkeypatch):
    def _fake_spawn(params, ctx):
        return ToolResult(content='ok', details={'contract_id': 'ctr-3'})

    def _fake_get_contract(state_dir, contract_id):
        return {
            'status': 'completed',
            'phases': [{'result_summary': 'result text'}],
            'metadata': {'topology_depth': 2, 'topology_budget': {'root_id': 'AG-ROOT'}},
        }

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)
    monkeypatch.setattr('charon.shade.shade_orchestrator.get_contract', _fake_get_contract)

    state_dir = tmp_path / 'state'
    _pykernel_worker._bridge_module(str(tmp_path), str(state_dir), 'AG-ROOT').rlm('trace me', poll_interval=0)

    trace_path = state_dir / 'rlm' / 'AG-ROOT.jsonl'
    assert trace_path.exists()
    record = json.loads(trace_path.read_text().strip().splitlines()[-1])
    assert record['id'] == 'ctr-3'
    assert record['status'] == 'completed'
    assert record['depth'] == 2
    assert record['output_ref'] == 'ctr-3'
