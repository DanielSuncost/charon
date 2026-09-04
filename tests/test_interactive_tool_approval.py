"""Concurrency and lifecycle tests for targeted interactive approvals."""

from __future__ import annotations

import threading
import time

import charon.tools as tools_module
from charon.tools import (
    ToolContext,
    _request_interactive_approval,
    execute_tool,
    respond_to_approval,
    set_approval_callback,
)


def teardown_function():
    set_approval_callback(None)


def _wait_for_requests(events: list[dict], condition: threading.Condition, count: int) -> list[dict]:
    deadline = time.monotonic() + 2
    with condition:
        while True:
            requests = [event for event in events if event.get('type') == 'approval_request']
            if len(requests) >= count:
                return requests
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f'timed out waiting for {count} approval requests')
            condition.wait(timeout=remaining)


def test_concurrent_responses_only_release_matching_request(tmp_path):
    events: list[dict] = []
    condition = threading.Condition()
    results: dict[str, tuple[bool, str]] = {}

    def callback(event: dict) -> None:
        with condition:
            events.append(event)
            condition.notify_all()

    def request(label: str) -> None:
        ctx = ToolContext(
            project_root=tmp_path,
            state_dir=tmp_path,
            agent_id=f'agent-{label}',
            operation_id='op-test',
            operation_domain='research',
            work_unit_id=label,
        )
        results[label] = _request_interactive_approval(
            'Http',
            {'method': 'POST', 'url': f'https://example.test/{label}'},
            'network',
            f'Http: {label}',
            ctx,
            timeout_seconds=2,
        )

    set_approval_callback(callback)
    first = threading.Thread(target=request, args=('first',), daemon=True)
    second = threading.Thread(target=request, args=('second',), daemon=True)
    first.start()
    second.start()

    requests = _wait_for_requests(events, condition, 2)
    request_ids = {
        event['work_unit_id']: event['approval_id']
        for event in requests
    }
    assert request_ids['first'] != request_ids['second']

    assert respond_to_approval(request_ids['first'], True)
    first.join(timeout=1)
    assert not first.is_alive()
    assert second.is_alive(), 'resolving one approval must not release another waiter'
    assert results['first'] == (True, 'approved')

    assert respond_to_approval(request_ids['second'], False)
    second.join(timeout=1)
    assert not second.is_alive()
    assert results['second'] == (False, 'denied')

    resolutions = {
        event['approval_id']: event['resolution']
        for event in events
        if event.get('type') == 'approval_resolved'
    }
    assert resolutions == {
        request_ids['first']: 'approved',
        request_ids['second']: 'denied',
    }


def test_timeout_emits_targeted_resolution(tmp_path):
    events: list[dict] = []
    set_approval_callback(events.append)
    ctx = ToolContext(
        project_root=tmp_path,
        state_dir=tmp_path,
        agent_id='agent-timeout',
        operation_id='op-timeout',
    )

    result = _request_interactive_approval(
        'Http',
        {'method': 'POST', 'url': 'https://example.test/write'},
        'network',
        'external mutation',
        ctx,
        timeout_seconds=0.01,
    )

    assert result == (False, 'timeout')
    request = next(event for event in events if event['type'] == 'approval_request')
    resolution = next(event for event in events if event['type'] == 'approval_resolved')
    assert resolution['approval_id'] == request['approval_id']
    assert resolution['resolution'] == 'timeout'
    assert resolution['approved'] is False


def test_missing_interactive_sink_fails_closed_for_gated_action(tmp_path):
    set_approval_callback(None)
    ctx = ToolContext(project_root=tmp_path, state_dir=tmp_path, agent_id='agent')
    assert _request_interactive_approval(
        'Bash',
        {'command': 'rm -rf /'},
        'dangerous',
        'recursive delete',
        ctx,
        timeout_seconds=0,
    ) == (False, 'unavailable')


def test_execute_tool_auto_runs_background_research_source_without_prompt(
    monkeypatch,
    tmp_path,
):
    events: list[dict] = []
    set_approval_callback(events.append)
    monkeypatch.setitem(
        tools_module.TOOL_EXECUTORS,
        'Web',
        lambda params, ctx: tools_module.ToolResult(content='source result'),
    )
    ctx = ToolContext(
        project_root=tmp_path,
        state_dir=tmp_path,
        agent_id='researcher',
        operation_id='op-research',
        operation_domain='research',
        runtime_role='background_agent',
    )

    result = execute_tool(
        'Web',
        {'action': 'search', 'query': 'recent methods'},
        ctx,
    )
    assert result.content == 'source result'
    assert not result.is_error
    assert events == []


def test_execute_tool_keeps_dangerous_gate_for_scoped_background_agent(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv('CHARON_SKIP_APPROVAL', raising=False)
    set_approval_callback(None)
    ctx = ToolContext(
        project_root=tmp_path,
        state_dir=tmp_path,
        agent_id='scoped-researcher',
        operation_id='op-research',
        operation_domain='research',
        runtime_role='background_agent',
        scope=['research'],
    )

    result = execute_tool('Bash', {'command': 'rm -rf /'}, ctx)
    assert result.is_error
    assert 'approval unavailable' in result.content
