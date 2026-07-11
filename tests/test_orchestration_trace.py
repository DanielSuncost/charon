"""Unified orchestration trace/span substrate: span lifecycle, correlation IDs,
coalescing reader, span tree, cost estimation, cross-system timeline."""
from pathlib import Path

import pytest

from charon.infra import orchestration_trace as ot


def test_cost_estimation_tiers_models_and_fallback():
    assert ot.estimate_cost_usd('local', 1_000_000, 1_000_000) == 0.0
    assert ot.estimate_cost_usd('strong', 1_000_000, 0) == 3.0
    # real model prefix match (gpt-5.5 → 1.25 in / 10 out per 1M)
    assert ot.estimate_cost_usd('gpt-5.5', 1_000_000, 1_000_000) == round(1.25 + 10.0, 6)
    assert ot.estimate_cost_usd('claude-opus-4-8', 1_000_000, 0) == 15.0
    # unknown model falls back to 'fast' tier, never crashes
    assert ot.estimate_cost_usd('some-unknown-model', 1_000_000, 0) == 0.15
    assert ot.estimate_cost_usd('', 0, 0) == 0.0


def test_span_context_manager_writes_start_and_end(tmp_path):
    tid = ot.new_trace_id()
    with ot.span(tmp_path, name='scout', system='libris', kind='agent_run',
                 trace_id=tid, operation_id='op1', agent_id='AG-1') as sp:
        sp.add_usage(model='gpt-5.5', input_tokens=1000, output_tokens=200)
    spans = ot.read_spans(tmp_path, trace_id=tid)
    assert len(spans) == 1                      # start+end coalesced to one
    s = spans[0]
    assert s['status'] == 'ok'
    assert s['operation_id'] == 'op1' and s['agent_id'] == 'AG-1'
    assert s['total_tokens'] == 1200
    assert s['cost_usd'] > 0                     # auto-estimated
    assert s['duration_ms'] is not None and s['duration_ms'] >= 0


def test_span_records_error_and_reraises(tmp_path):
    tid = ot.new_trace_id()
    with pytest.raises(ValueError):
        with ot.span(tmp_path, name='boom', system='devop', kind='step', trace_id=tid):
            raise ValueError('kaboom')
    s = ot.read_spans(tmp_path, trace_id=tid)[0]
    assert s['status'] == 'error'
    assert 'kaboom' in s['error']


def test_record_span_oneshot_with_duration(tmp_path):
    tid = ot.new_trace_id()
    sp = ot.record_span(tmp_path, name='tool: Web', system='libris', kind='tool_call',
                        trace_id=tid, duration_ms=812.0, status='ok')
    assert sp.duration_ms == 812.0
    s = ot.read_spans(tmp_path, trace_id=tid)[0]
    assert s['kind'] == 'tool_call' and s['duration_ms'] == 812.0


def test_coalesce_terminal_status_wins(tmp_path):
    # a running row followed by an ok row for the same span → one 'ok' span
    tid = ot.new_trace_id()
    with ot.span(tmp_path, name='x', system='judge', kind='step', trace_id=tid):
        pass
    rows = (tmp_path / 'traces' / 'spans.jsonl').read_text().strip().splitlines()
    assert len(rows) == 2                        # start + end written
    spans = ot.read_spans(tmp_path, trace_id=tid)
    assert len(spans) == 1 and spans[0]['status'] == 'ok'


def test_span_tree_nesting(tmp_path):
    tid = ot.new_trace_id()
    with ot.span(tmp_path, name='op', system='libris', kind='operation',
                 trace_id=tid, span_id='sp_root'):
        pass
    ot.record_span(tmp_path, name='child1', system='libris', kind='agent_run',
                   trace_id=tid, span_id='sp_c1', parent_span_id='sp_root')
    ot.record_span(tmp_path, name='child2', system='libris', kind='tool_call',
                   trace_id=tid, span_id='sp_c2', parent_span_id='sp_c1')
    tree = ot.build_span_tree(ot.read_spans(tmp_path, trace_id=tid))
    assert len(tree) == 1 and tree[0]['span_id'] == 'sp_root'
    assert tree[0]['children'][0]['span_id'] == 'sp_c1'
    assert tree[0]['children'][0]['children'][0]['span_id'] == 'sp_c2'


def test_trace_summary_rollup(tmp_path):
    tid = ot.new_trace_id()
    with ot.span(tmp_path, name='a', system='libris', kind='agent_run', trace_id=tid) as s:
        s.add_usage(model='strong', input_tokens=1_000_000, output_tokens=0)  # $3
    ot.record_span(tmp_path, name='b', system='libris', kind='tool_call', trace_id=tid,
                   status='error', error='nope')
    summ = ot.trace_summary(tmp_path, tid)
    assert summ['spans'] == 2
    assert summ['total_tokens'] == 1_000_000
    assert summ['cost_usd'] == 3.0
    assert summ['errors'] == 1
    assert summ['by_system'] == {'libris': 2}


def test_timeline_is_cross_system_and_chronological(tmp_path):
    ot.record_span(tmp_path, name='libris step', system='libris', kind='step')
    ot.record_span(tmp_path, name='devop step', system='devop', kind='step')
    ot.record_span(tmp_path, name='batch task', system='batch', kind='agent_run')
    tl = ot.timeline(tmp_path)
    systems = [row['system'] for row in tl]
    assert set(systems) == {'libris', 'devop', 'batch'}
    # chronological by start_ts
    starts = [row['start_ts'] for row in tl]
    assert starts == sorted(starts)


def test_tracing_never_raises_on_bad_state_dir():
    # persistence failures must be swallowed; span still yields a usable object
    with ot.span(Path('/nonexistent/dir/xyz'), name='x', system='libris', kind='step') as sp:
        sp.add_usage(model='fast', input_tokens=10, output_tokens=10)
    assert sp.status == 'ok'


def test_producer_execution_memory_tool_event_emits_span(tmp_path, monkeypatch):
    monkeypatch.setenv('CHARON_NO_SQLITE', '1')
    from charon.memory import execution_memory as em
    em.record_tool_event(
        tmp_path, session_id='sess-9', agent_id='AG-9', provider='codex',
        tool_name='Bash', params={'command': 'ls'}, result_content='ok',
        is_error=False, project_root=str(tmp_path), duration_ms=42)
    spans = ot.read_spans(tmp_path, trace_id='tr_sess-9')
    assert len(spans) == 1
    s = spans[0]
    assert s['system'] == 'agent' and s['kind'] == 'tool_call'
    assert s['agent_id'] == 'AG-9' and s['duration_ms'] == 42.0
    assert s['attributes']['tool'] == 'Bash'


def test_producer_shade_phase_event_emits_span(tmp_path, monkeypatch):
    monkeypatch.setenv('CHARON_NO_SQLITE', '1')
    from charon.shade import shade_orchestrator as so
    so.append_phase_event(tmp_path, contract_id='ctr-1', phase_id='P02',
                          event_type='phase_completed', payload={'x': 1})
    # a non-terminal event should NOT emit a span
    so.append_phase_event(tmp_path, contract_id='ctr-1', phase_id='P03',
                          event_type='phase_queued')
    spans = ot.read_spans(tmp_path, trace_id='tr_ctr-1')
    assert len(spans) == 1
    assert spans[0]['system'] == 'shade' and spans[0]['kind'] == 'phase'
    assert spans[0]['task_id'] == 'P02' and spans[0]['status'] == 'ok'


def test_producers_share_cross_system_timeline(tmp_path, monkeypatch):
    monkeypatch.setenv('CHARON_NO_SQLITE', '1')
    from charon.memory import execution_memory as em
    from charon.shade import shade_orchestrator as so
    em.record_tool_event(tmp_path, session_id='s', agent_id='A', provider='p',
                         tool_name='Read', params={}, result_content='', is_error=False,
                         project_root=str(tmp_path))
    so.append_phase_event(tmp_path, contract_id='c', phase_id='P1',
                         event_type='phase_completed')
    systems = {row['system'] for row in ot.timeline(tmp_path)}
    assert {'agent', 'shade'} <= systems
