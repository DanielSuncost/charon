"""Registered, managed-process software teams shared by chat and /devop."""
import json
import os
from pathlib import Path
import shlex
import sys

from charon.tools import ToolContext, ToolResult

SOFTWARE_TEAM_TOOL_DEF = {
    'name': 'SoftwareTeam',
    'description': 'Start a registered software development operation visible in F4 for its target project, or inspect status. Use this instead of standalone SpawnShade for a software team. Runs in a managed process with checkpoint and review gates.',
    'input_schema': {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['start', 'status']},
        'project_root': {'type': 'string'}, 'prompt': {'type': 'string'},
        'operation_id': {'type': 'string'},
    }, 'required': ['action']},
}


def launch_software_team(state_dir, project_root, *, prompt, parent_agent_id='', budget=None, policy=None, max_workstreams=4):
    from charon.devop.devop_runtime import init_operation, operation_dir, set_operation_status
    from charon.tools import execute_run_process
    root = Path(project_root).expanduser().resolve()
    state = Path(state_dir).resolve()
    if not root.is_dir() or not str(prompt).strip():
        raise ValueError('Existing project_root and nonempty prompt required.')
    op = init_operation(state, root, prompt=prompt, title=prompt[:120], budget=budget, policy=policy)
    set_operation_status(state, op['operation_id'], 'starting', 'Starting managed software team.')
    source = str(Path(__file__).resolve().parents[2])
    pythonpath = source + (os.pathsep + os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')
    cmd = shlex.join(['env', 'PYTHONPATH=' + pythonpath, 'CHARON_STATE_DIR=' + str(state),
                      sys.executable, '-m', 'charon.devop.devop_runner', '--state-dir', str(state),
                      '--project-root', str(root), '--operation-id', op['operation_id'],
                      '--max-workstreams', str(max_workstreams), '--parent-agent-id', parent_agent_id])
    ctx = ToolContext(project_root=root, state_dir=state, agent_id=parent_agent_id)
    try:
        result = execute_run_process({'command': cmd, 'cwd': str(root), 'name': 'software-team-' + op['operation_id']}, ctx)
        if result.is_error:
            raise RuntimeError(result.content)
        # Separate launch metadata avoids racing operation status updates in child.
        (operation_dir(state, op['operation_id']) / 'launch.json').write_text(json.dumps(result.details, default=str, indent=2))
    except Exception as exc:
        set_operation_status(state, op['operation_id'], 'failed', str(exc))
        raise
    return {'operation': {**op, 'status': 'starting'}, 'coordinator': {}, 'process': result.details}


def execute_software_team(params: dict, ctx: ToolContext) -> ToolResult:
    from charon.devop.devop_runtime import get_operation_state
    state = ctx.state_dir or ctx.project_root / '.charon_state'
    action = params.get('action')
    if action == 'status':
        op_id = str(params.get('operation_id') or '')
        if not op_id or Path(op_id).name != op_id:
            return ToolResult(content='Valid operation_id required.', is_error=True)
        op = get_operation_state(state, op_id)
        return ToolResult(content=json.dumps(op, default=str), is_error=not bool(op))
    if action != 'start':
        return ToolResult(content='Unknown action; use start or status.', is_error=True)
    try:
        result = launch_software_team(state, params.get('project_root') or ctx.project_root,
                                     prompt=str(params.get('prompt') or '').strip(), parent_agent_id=ctx.agent_id)
        op = result['operation']
        return ToolResult(content=f"Software team starting (not yet verified running).\nOperation: {op['operation_id']}\nF4 project: {op['project_root']}\nProcess: {result['process'].get('process_id')}", details=result)
    except Exception as exc:
        return ToolResult(content=f'Software team startup failed: {exc}', is_error=True)
