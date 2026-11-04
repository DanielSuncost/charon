"""Managed host for software teams; never relies on the initiating chat thread."""
import argparse
import os
from pathlib import Path

from charon.devop.devop_runtime import get_operation_state, set_operation_status
from charon.devop.devop_agents import spawn_devop_role, _run_operation_controller
from charon.devop.managed_host import run_managed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--operation-id', required=True)
    parser.add_argument('--max-workstreams', type=int, default=4)
    parser.add_argument('--parent-agent-id', default='')
    args = parser.parse_args()
    os.environ['CHARON_STATE_DIR'] = str(args.state_dir.resolve())
    op = get_operation_state(args.state_dir, args.operation_id)
    if not op:
        raise SystemExit('Unknown operation')
    if Path(op['project_root']).resolve() != args.project_root.resolve():
        raise SystemExit('Operation project mismatch')

    def body():
        set_operation_status(args.state_dir, args.operation_id, 'running', 'Managed runner started; coordinator preparing workstreams.')
        coordinator = spawn_devop_role(args.state_dir, args.project_root, role='coordinator', operation_id=args.operation_id,
                                       user_goal=op['prompt'], parent_agent_id=args.parent_agent_id)
        print(f"Software operation {args.operation_id} registered for {args.project_root}", flush=True)
        coordinator['_thread'].join()
        if get_operation_state(args.state_dir, args.operation_id).get('status') != 'failed':
            _run_operation_controller(args.state_dir, args.project_root, args.operation_id, op['prompt'], coordinator, args.max_workstreams)
        status = get_operation_state(args.state_dir, args.operation_id).get('status', 'unknown')
        print('Final status: ' + status, flush=True)
        if status == 'failed':
            raise SystemExit(1)

    run_managed(
        body,
        stop_requested=lambda: bool(get_operation_state(args.state_dir, args.operation_id).get('stop_requested')),
        on_stop=lambda: set_operation_status(args.state_dir, args.operation_id, 'stopped', 'Runner stopped; partial artifacts retained.'),
        on_failure=lambda exc: set_operation_status(args.state_dir, args.operation_id, 'failed', str(exc)),
    )


if __name__ == '__main__':
    main()
