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
            'shade_agent_id': 'AG-X',
        }

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)
    monkeypatch.setattr('charon.shade.shade_orchestrator.get_contract', _fake_get_contract)

    result = _mod(tmp_path).rlm('do the thing', poll_interval=0)

    assert result == {'status': 'completed', 'output': 'the final answer', 'contract_id': 'ctr-1', 'shade_id': 'AG-X'}
    assert calls['n'] >= 2  # actually polled more than once before hitting a terminal status


def test_rlm_returns_last_error_as_output_on_failure(tmp_path, monkeypatch):
    def _fake_spawn(params, ctx):
        return ToolResult(content='ok', details={'contract_id': 'ctr-2'})

    def _fake_get_contract(state_dir, contract_id):
        return {'status': 'failed', 'phases': [], 'last_error': 'boom', 'metadata': {}, 'shade_agent_id': 'AG-Y'}

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)
    monkeypatch.setattr('charon.shade.shade_orchestrator.get_contract', _fake_get_contract)

    result = _mod(tmp_path).rlm('do the thing', poll_interval=0)
    assert result == {'status': 'failed', 'output': 'boom', 'contract_id': 'ctr-2', 'shade_id': 'AG-Y'}


def test_rlm_raises_when_spawn_fails(tmp_path, monkeypatch):
    def _fake_spawn(params, ctx):
        return ToolResult(content='no provider configured', is_error=True)

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)

    try:
        _mod(tmp_path).rlm('do the thing')
        raise AssertionError('expected RuntimeError')
    except RuntimeError as e:
        assert 'no provider configured' in str(e)


def test_rlm_retain_passes_through_to_fresh_spawn(tmp_path, monkeypatch):
    """rlm(..., retain=True) must actually request a retained shade — the
    only way to later reactivate it via rlm(child_agent_id=...)."""
    captured_params = {}

    def _fake_spawn(params, ctx):
        captured_params.update(params)
        return ToolResult(content='ok', details={'contract_id': 'ctr-4', 'shade_id': 'AG-RETAINED'})

    def _fake_get_contract(state_dir, contract_id):
        return {'status': 'completed', 'phases': [{'result_summary': 'done'}], 'metadata': {}, 'shade_agent_id': 'AG-RETAINED'}

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)
    monkeypatch.setattr('charon.shade.shade_orchestrator.get_contract', _fake_get_contract)

    result = _mod(tmp_path).rlm('do the thing', retain=True, poll_interval=0)

    assert captured_params['retain'] is True
    assert result['shade_id'] == 'AG-RETAINED'


def test_rlm_task_complexity_passes_through_to_fresh_spawn(tmp_path, monkeypatch):
    captured_params = {}

    def _fake_spawn(params, ctx):
        captured_params.update(params)
        return ToolResult(content='ok', details={'contract_id': 'ctr-6'})

    def _fake_get_contract(state_dir, contract_id):
        return {'status': 'completed', 'phases': [{'result_summary': 'done'}], 'metadata': {}, 'shade_agent_id': 'AG-Z'}

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)
    monkeypatch.setattr('charon.shade.shade_orchestrator.get_contract', _fake_get_contract)

    _mod(tmp_path).rlm('do the thing', task_complexity='complex', poll_interval=0)

    assert captured_params['task_complexity'] == 'complex'


def test_rlm_child_agent_id_reactivates_and_blocks(tmp_path, monkeypatch):
    calls = {'n': 0}

    def _fake_reactivate(state_dir, child_agent_id, objective, ctx, *, depth, budget):
        assert child_agent_id == 'AG-RETAINED'
        assert objective == 'follow-up instruction'
        return {'shade_id': child_agent_id, 'contract_id': 'ctr-reactivated', 'status': 'running'}

    def _fake_get_contract(state_dir, contract_id):
        calls['n'] += 1
        status = 'completed' if calls['n'] >= 2 else 'running'
        return {
            'status': status, 'phases': [{'result_summary': 'resumed answer'}], 'metadata': {},
            'shade_agent_id': 'AG-RETAINED',
        }

    monkeypatch.setattr('charon.tools.shade_tool.execute_reactivate_shade', _fake_reactivate)
    monkeypatch.setattr('charon.shade.shade_orchestrator.get_contract', _fake_get_contract)

    result = _mod(tmp_path).rlm('follow-up instruction', child_agent_id='AG-RETAINED', poll_interval=0)

    assert result == {
        'status': 'completed', 'output': 'resumed answer', 'contract_id': 'ctr-reactivated', 'shade_id': 'AG-RETAINED',
    }


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


def _setup_completed_spawn(monkeypatch, output_text='a genuine finding'):
    def _fake_spawn(params, ctx):
        return ToolResult(content='ok', details={'contract_id': 'ctr-5'})

    def _fake_get_contract(state_dir, contract_id):
        return {
            'status': 'completed', 'phases': [{'result_summary': output_text}],
            'metadata': {}, 'shade_agent_id': 'AG-PROMO',
        }

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)
    monkeypatch.setattr('charon.shade.shade_orchestrator.get_contract', _fake_get_contract)
    monkeypatch.setattr(
        'charon.providers.model_registry.get_shade_provider_and_model',
        lambda *a, **k: ('fake-provider', 'fake-model', {}),
    )


def test_rlm_promote_writes_memory_when_judge_scores_high(tmp_path, monkeypatch):
    from charon.judge.judge_engine import JudgeVerdict

    _setup_completed_spawn(monkeypatch)
    monkeypatch.setattr(
        'charon.judge.judge_engine.score_text',
        lambda text, **k: JudgeVerdict(score=9.0, feedback='genuine finding'),
    )
    added = {}

    class _FakeMemoryEngine:
        def __init__(self, state_dir):
            pass

        def add(self, content, **kwargs):
            added['content'] = content
            added['kwargs'] = kwargs

        def close(self):
            pass

    monkeypatch.setattr('charon.memory.memory_engine.MemoryEngine', _FakeMemoryEngine)

    result = _mod(tmp_path).rlm('find something', promote=True, poll_interval=0)

    assert result['promoted'] is True
    assert result['promotion_score'] == 9.0
    assert added['content'] == 'a genuine finding'
    assert added['kwargs']['tier'] == 'project'
    assert added['kwargs']['source_agent'] == 'AG-PROMO'


def test_rlm_promote_skips_memory_when_judge_scores_low(tmp_path, monkeypatch):
    from charon.judge.judge_engine import JudgeVerdict

    _setup_completed_spawn(monkeypatch)
    monkeypatch.setattr(
        'charon.judge.judge_engine.score_text',
        lambda text, **k: JudgeVerdict(score=3.0, feedback='routine, not worth keeping'),
    )
    memory_calls = {'n': 0}

    class _FakeMemoryEngine:
        def __init__(self, state_dir):
            memory_calls['n'] += 1

    monkeypatch.setattr('charon.memory.memory_engine.MemoryEngine', _FakeMemoryEngine)

    result = _mod(tmp_path).rlm('find something', promote=True, poll_interval=0)

    assert result['promoted'] is False
    assert result['promotion_score'] == 3.0
    assert memory_calls['n'] == 0


def test_rlm_without_promote_has_no_promotion_keys(tmp_path, monkeypatch):
    _setup_completed_spawn(monkeypatch)
    result = _mod(tmp_path).rlm('find something', poll_interval=0)
    assert 'promoted' not in result
    assert 'promotion_score' not in result


def test_rlm_promote_never_fails_the_call_if_judging_errors(tmp_path, monkeypatch):
    _setup_completed_spawn(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError('judge unavailable')

    monkeypatch.setattr('charon.judge.judge_engine.score_text', _boom)

    result = _mod(tmp_path).rlm('find something', promote=True, poll_interval=0)
    assert result['status'] == 'completed'
    assert result['promoted'] is False


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
