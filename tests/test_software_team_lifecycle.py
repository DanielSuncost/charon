"""Regression checks for the software-team launch, evidence and failure path."""
import json
import os
import shlex
from types import SimpleNamespace

import pytest

from charon.devop import devop_agents as agents
from charon.devop import devop_runtime as runtime
from charon.tools import ToolContext, ToolResult
from charon.tools.software_team_tool import execute_software_team
from chat_backend import _collect_devop_rooms


def test_launch_uses_explicit_project_state_and_import_path(tmp_path, monkeypatch):
    import charon.tools as tools
    target = tmp_path / 'pomemo with spaces'
    target.mkdir()
    state = tmp_path / 'shared-state'
    ctx = ToolContext(project_root=tmp_path, state_dir=state)
    calls = []

    def launch(params, context):
        calls.append((params, context))
        return ToolResult(content='started', details={'process_id': 'proc-test', 'pid': os.getpid()})

    monkeypatch.setattr(tools, 'execute_run_process', launch)
    result = execute_software_team({'action': 'start', 'project_root': str(target), 'prompt': 'Build iOS app'}, ctx)
    assert not result.is_error
    op_id = result.details['operation']['operation_id']
    cmd = shlex.split(calls[0][0]['command'])
    assert any(arg.startswith('PYTHONPATH=') and '/src' in arg for arg in cmd)
    assert 'CHARON_STATE_DIR=' + str(state) in cmd
    assert cmd[cmd.index('--project-root') + 1] == str(target)
    assert calls[0][1].state_dir == state
    assert runtime.get_operation_state(state, op_id)['status'] == 'starting'
    rooms = _collect_devop_rooms(state, target)
    assert rooms[0]['operation_id'] == op_id
    assert _collect_devop_rooms(state, tmp_path) == []
    assert json.loads((runtime.operation_dir(state, op_id) / 'launch.json').read_text())['pid'] == os.getpid()


def test_failed_process_start_remains_visible(tmp_path, monkeypatch):
    import charon.tools as tools
    monkeypatch.setattr(tools, 'execute_run_process', lambda *a: ToolResult(content='cannot spawn', is_error=True))
    result = execute_software_team({'action': 'start', 'prompt': 'Build app'}, ToolContext(project_root=tmp_path, state_dir=tmp_path / 'state'))
    assert result.is_error
    assert _collect_devop_rooms(tmp_path / 'state', tmp_path)[0]['status'] == 'failed'


@pytest.mark.parametrize('event_type', ['error', 'budget_stop'])
def test_worker_error_event_cannot_be_success(tmp_path, monkeypatch, event_type):
    op = runtime.init_operation(tmp_path, tmp_path, prompt='build')
    ws = runtime.init_workstream(tmp_path, op['operation_id'], title='Engine')

    class Engine:
        async def submit_and_collect(self, instruction):
            return 'Some partial work', [SimpleNamespace(type=event_type, data={'error': 'interrupted'})]

    monkeypatch.setattr(agents, 'create_devop_engine', lambda *a, **k: (Engine(), SimpleNamespace(model_id='test')))
    agents._run_devop_role(tmp_path, tmp_path, {'id': 'test'}, 'implementer', op['operation_id'], ws['slug'], 'build')
    actual = runtime.get_operation_state(tmp_path, op['operation_id'])
    if event_type == 'error':
        assert actual['status'] == 'failed'
        assert actual['workstreams'][0]['status'] == 'failed'
    else:
        # A guard-stopped (wedged) pass is incomplete, not success, and does
        # not fail the whole operation.
        assert actual['status'] != 'failed'
        assert actual['workstreams'][0]['status'] == 'incomplete'
    assert not actual['workstreams'][0]['checkpoints']
    assert not any(e['kind'] == 'implementer_completed' for e in actual['events_tail'])


def test_checkpoint_precedes_review_and_lanes_do_not_finish_as_success(tmp_path, monkeypatch):
    op = runtime.init_operation(tmp_path, tmp_path, prompt='Build iOS app')
    calls = []

    class Engine:
        def __init__(self, role): self.role = role
        async def submit_and_collect(self, instruction):
            if self.role == 'judge':
                assert 'Implementation report:' in instruction
                assert 'build evidence' in instruction
                return 'Checked scope evidence\nVERDICT: approve', []
            return 'Implementation report with build evidence', []

    monkeypatch.setattr(agents, 'create_devop_engine', lambda *a, **k: (Engine(k['role']), SimpleNamespace(model_id='test')))

    def spawn(state, root, **kw):
        calls.append(kw['role'])
        role, slug = kw['role'], kw['workstream_slug']
        fields = {'owner_agent_id': role} if role == 'implementer' else {'paired_judge_agent_id': role}
        runtime.update_workstream_runtime(state, kw['operation_id'], slug, **fields)
        # Run immediately to exercise the fast-result registration race.
        agents._run_devop_role(state, root, {'id': role}, role, kw['operation_id'], slug, kw['user_goal'])
        return {'id': role, '_thread': SimpleNamespace(join=lambda: None)}

    monkeypatch.setattr(agents, 'spawn_devop_role', spawn)
    agents._run_operation_controller(tmp_path, tmp_path, op['operation_id'], op['prompt'], {'id': 'coordinator'}, 4)
    actual = runtime.get_operation_state(tmp_path, op['operation_id'])
    assert calls == ['implementer', 'judge'] * 3
    assert actual['status'] == 'needs_review'
    assert all(ws['latest_checkpoint'] and ws['latest_review']['decision'] == 'approve' for ws in actual['workstreams'])
    room = _collect_devop_rooms(tmp_path, tmp_path)[0]
    assert room['workstreams'] and room['events']


def test_fast_failure_is_not_overwritten_and_stops_further_lanes(tmp_path, monkeypatch):
    op = runtime.init_operation(tmp_path, tmp_path, prompt='Build iOS app')
    calls = []
    def spawn(state, root, **kw):
        calls.append(kw['role'])
        runtime.update_workstream_runtime(state, kw['operation_id'], kw['workstream_slug'], status='failed')
        return {'id': 'failed', '_thread': SimpleNamespace(join=lambda: None)}
    monkeypatch.setattr(agents, 'spawn_devop_role', spawn)
    agents._run_operation_controller(tmp_path, tmp_path, op['operation_id'], op['prompt'], {}, 4)
    actual = runtime.get_operation_state(tmp_path, op['operation_id'])
    assert actual['status'] == 'failed'
    assert calls == ['implementer']
    assert sorted(ws['status'] for ws in actual['workstreams']) == ['failed', 'pending', 'pending']


def test_unknown_action_does_not_launch(tmp_path):
    result = execute_software_team({'action': 'oops'}, ToolContext(project_root=tmp_path))
    assert result.is_error
    assert not (tmp_path / '.charon_state').exists()


def test_dead_runner_is_not_left_running(tmp_path, monkeypatch):
    op = runtime.init_operation(tmp_path, tmp_path, prompt='build')
    (runtime.operation_dir(tmp_path, op['operation_id']) / 'launch.json').write_text(json.dumps({'pid': 99999999}))
    def missing(*args):
        raise ProcessLookupError()
    monkeypatch.setattr(os, 'kill', missing)
    assert runtime.get_operation_state(tmp_path, op['operation_id'])['status'] == 'failed'
    assert _collect_devop_rooms(tmp_path, tmp_path)[0]['status'] == 'failed'


def test_interrupted_shade_is_not_completed(tmp_path):
    from charon.shade import shade_orchestrator as shades
    contract = shades.create_contract(tmp_path, parent_task_id='t', parent_agent_id='a',
                                      shade_agent_id='s', conversation_id='c', project=str(tmp_path), goal='build')
    summary = 'Stopped: task time budget exhausted. Existing artifacts are retained. Task is incomplete; do not treat prior claims as final verification.'
    result = shades.mark_phase_completed(tmp_path, contract['id'], 'P01', task_id='t', summary=summary)
    assert result['status'] == 'failed'
    assert result['phases'][0]['status'] == 'failed'
    assert not any(e['event_type'] == 'contract_completed' for e in shades.load_phase_events(tmp_path, contract['id']))


def test_public_api_uses_same_managed_launcher(tmp_path, monkeypatch):
    from charon.tools import software_team_tool
    monkeypatch.setattr(software_team_tool, 'launch_software_team', lambda *a, **kw: {'managed': True})
    assert agents.start_autonomous_software_operation(tmp_path, tmp_path, prompt='build') == {'managed': True}
