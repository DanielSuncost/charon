"""Tests for _pykernel_worker.rlm() — the blocking recursive-call primitive.

Exercised directly against the bridge-module closure (no real kernel
subprocess needed), with execute_spawn_shade/get_contract mocked — mirrors
how test_tools_pykernel.py checks `callable(charon.spawn_shade)` without a
live provider, just going one level deeper into the actual polling logic.
"""
import json
import time

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


# ---------------------------------------------------------------------------
# Phase D: a kernel's inherited topology_depth/topology_budget (from a shade
# that itself calls PyKernel) must actually flow into rlm()'s spawns and the
# trace log's root_task_id, not silently root a fresh tree at this kernel's
# own agent_id.
# ---------------------------------------------------------------------------

def test_bridge_module_inherits_topology_context_for_fresh_spawn(tmp_path, monkeypatch):
    captured = {}

    def _fake_spawn(params, ctx):
        captured['ctx'] = ctx
        return ToolResult(content='ok', details={'contract_id': 'ctr-7', 'shade_id': 'AG-CHILD'})

    def _fake_get_contract(state_dir, contract_id):
        return {
            'status': 'completed', 'phases': [{'result_summary': 'done'}],
            'metadata': {'topology_depth': 3, 'topology_budget': {'root_id': 'ROOT-X'}},
            'shade_agent_id': 'AG-CHILD',
        }

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)
    monkeypatch.setattr('charon.shade.shade_orchestrator.get_contract', _fake_get_contract)

    inherited_budget = {'root_id': 'ROOT-X', 'max_depth': 5}
    state_dir = tmp_path / 'state'
    mod = _pykernel_worker._bridge_module(
        str(tmp_path), str(state_dir), 'AG-NESTED', topology_depth=2, topology_budget=inherited_budget,
    )
    mod.rlm('nested call', poll_interval=0)

    assert captured['ctx'].topology_depth == 2
    assert captured['ctx'].topology_budget == inherited_budget
    # Traced under the true tree root (ROOT-X), not this kernel's own
    # agent_id (AG-NESTED) — that's the fragmentation fix.
    assert (state_dir / 'rlm' / 'ROOT-X.jsonl').exists()
    assert not (state_dir / 'rlm' / 'AG-NESTED.jsonl').exists()


def test_bridge_module_defaults_to_fresh_tree_when_no_context_inherited(tmp_path, monkeypatch):
    """No regression for the common case: a top-level kernel with nothing
    inherited still traces under its own agent_id, same as before."""
    def _fake_spawn(params, ctx):
        return ToolResult(content='ok', details={'contract_id': 'ctr-9'})

    def _fake_get_contract(state_dir, contract_id):
        return {'status': 'completed', 'phases': [{'result_summary': 'done'}], 'metadata': {}}

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)
    monkeypatch.setattr('charon.shade.shade_orchestrator.get_contract', _fake_get_contract)

    state_dir = tmp_path / 'state'
    _mod(tmp_path).rlm('plain call', poll_interval=0)

    assert (state_dir / 'rlm' / 'AG-ROOT.jsonl').exists()


# ---------------------------------------------------------------------------
# Phase E: rlm() self-limits its own wait to a margin under this call's
# timeout_sec, returning a clean 'still_running' result instead of relying
# on PyKernel's SIGINT to cut it off.
# ---------------------------------------------------------------------------

def test_rlm_returns_still_running_before_hard_timeout(tmp_path, monkeypatch):
    def _fake_spawn(params, ctx):
        return ToolResult(content='ok', details={'contract_id': 'ctr-8'})

    def _fake_get_contract(state_dir, contract_id):
        return {'status': 'running', 'phases': [], 'metadata': {}, 'shade_agent_id': 'AG-SLOW'}

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)
    monkeypatch.setattr('charon.shade.shade_orchestrator.get_contract', _fake_get_contract)

    mod = _mod(tmp_path)
    mod._call_state['deadline'] = time.time() + 1  # about to expire
    result = mod.rlm('slow thing', poll_interval=0)

    assert result == {'status': 'still_running', 'contract_id': 'ctr-8', 'shade_id': 'AG-SLOW'}


def test_rlm_blocks_normally_when_no_deadline_set(tmp_path, monkeypatch):
    """Backward compatible: a caller that never set _call_state (e.g. a
    direct test) gets the old unconditional-blocking behavior."""
    calls = {'n': 0}

    def _fake_spawn(params, ctx):
        return ToolResult(content='ok', details={'contract_id': 'ctr-10'})

    def _fake_get_contract(state_dir, contract_id):
        calls['n'] += 1
        status = 'completed' if calls['n'] >= 2 else 'running'
        return {'status': status, 'phases': [{'result_summary': 'done'}], 'metadata': {}}

    monkeypatch.setattr('charon.tools.shade_tool.execute_spawn_shade', _fake_spawn)
    monkeypatch.setattr('charon.shade.shade_orchestrator.get_contract', _fake_get_contract)

    result = _mod(tmp_path).rlm('quick thing', poll_interval=0)
    assert result['status'] == 'completed'


# ---------------------------------------------------------------------------
# Phase A: peer-to-peer agent messaging via charon.rlm(peer_agent_id=...)
# ---------------------------------------------------------------------------

def test_rlm_peer_agent_id_enqueues_with_attribution_and_blocks(tmp_path, monkeypatch):
    captured = {}
    calls = {'n': 0}

    def _fake_enqueue(state_dir, *, owner_agent_id, instruction):
        captured['owner_agent_id'] = owner_agent_id
        captured['instruction'] = instruction
        return {'id': 'task-abc123'}

    def _fake_load_queue(queue_file):
        calls['n'] += 1
        status = 'completed' if calls['n'] >= 2 else 'pending'
        return [{'id': 'task-abc123', 'status': status, 'result_summary': 'the peer replied'}]

    monkeypatch.setattr('charon.conversation.conversation_runtime.enqueue_agent_task', _fake_enqueue)
    monkeypatch.setattr('charon.infra.queue_io.load_queue_atomic', _fake_load_queue)

    result = _pykernel_worker._bridge_module(
        str(tmp_path), str(tmp_path / 'state'), 'AG-SENDER',
    ).rlm('are you free to review this?', peer_agent_id='AG-PEER', poll_interval=0)

    assert captured['owner_agent_id'] == 'AG-PEER'
    assert 'Message from agent AG-SENDER' in captured['instruction']
    assert 'are you free to review this?' in captured['instruction']
    assert result == {'status': 'completed', 'output': 'the peer replied', 'peer_task_id': 'task-abc123'}
    assert calls['n'] >= 2


def test_rlm_peer_task_id_resumes_without_reenqueueing(tmp_path, monkeypatch):
    enqueue_called = {'n': 0}

    def _fake_enqueue(*a, **k):
        enqueue_called['n'] += 1
        return {'id': 'should-not-happen'}

    def _fake_load_queue(queue_file):
        return [{'id': 'task-existing', 'status': 'completed', 'result_summary': 'done'}]

    monkeypatch.setattr('charon.conversation.conversation_runtime.enqueue_agent_task', _fake_enqueue)
    monkeypatch.setattr('charon.infra.queue_io.load_queue_atomic', _fake_load_queue)

    result = _mod(tmp_path).rlm('irrelevant once resuming', peer_task_id='task-existing', poll_interval=0)

    assert enqueue_called['n'] == 0
    assert result == {'status': 'completed', 'output': 'done', 'peer_task_id': 'task-existing'}


def test_rlm_peer_message_cap_refusal_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(
        'charon.agents.topology_budget.try_reserve_peer_message',
        lambda *a, **k: (False, 'peer-message cap reached'),
    )
    try:
        _mod(tmp_path).rlm('spam', peer_agent_id='AG-PEER')
        raise AssertionError('expected RuntimeError')
    except RuntimeError as e:
        assert 'cap' in str(e)


def test_rlm_peer_still_running_before_hard_timeout(tmp_path, monkeypatch):
    def _fake_enqueue(*a, **k):
        return {'id': 'task-slow'}

    def _fake_load_queue(queue_file):
        return [{'id': 'task-slow', 'status': 'pending'}]

    monkeypatch.setattr('charon.conversation.conversation_runtime.enqueue_agent_task', _fake_enqueue)
    monkeypatch.setattr('charon.infra.queue_io.load_queue_atomic', _fake_load_queue)

    mod = _mod(tmp_path)
    mod._call_state['deadline'] = time.time() + 1
    result = mod.rlm('slow peer', peer_agent_id='AG-PEER', poll_interval=0)

    assert result == {'status': 'still_running', 'peer_task_id': 'task-slow'}


def test_rlm_child_agent_id_and_peer_agent_id_mutually_exclusive(tmp_path):
    try:
        _mod(tmp_path).rlm('x', child_agent_id='AG-A', peer_agent_id='AG-B')
        raise AssertionError('expected ValueError')
    except ValueError as e:
        assert 'mutually exclusive' in str(e)


def test_rlm_contract_id_and_peer_task_id_mutually_exclusive(tmp_path):
    try:
        _mod(tmp_path).rlm('x', contract_id='c1', peer_task_id='t1')
        raise AssertionError('expected ValueError')
    except ValueError as e:
        assert 'mutually exclusive' in str(e)


def test_rlm_cannot_combine_fresh_target_with_resume_handle(tmp_path):
    try:
        _mod(tmp_path).rlm('x', child_agent_id='AG-A', contract_id='c1')
        raise AssertionError('expected ValueError')
    except ValueError as e:
        assert 'cannot combine' in str(e)
