from pathlib import Path
import argparse
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / 'scripts' / 'charon_agents.py'
SHADE_PATH = ROOT / 'apps' / 'core-daemon' / 'shade_orchestrator.py'

spec_cli = importlib.util.spec_from_file_location('charon_agents_cli_test', SCRIPT_PATH)
charon_agents = importlib.util.module_from_spec(spec_cli)
sys.modules[spec_cli.name] = charon_agents
spec_cli.loader.exec_module(charon_agents)

spec_shade = importlib.util.spec_from_file_location('shade_orchestrator_test_mod', SHADE_PATH)
shade_orchestrator = importlib.util.module_from_spec(spec_shade)
sys.modules[spec_shade.name] = shade_orchestrator
spec_shade.loader.exec_module(shade_orchestrator)


def test_shade_investigate_outputs_recommendation(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)

    charon_agents.STATE_DIR = state

    rec = shade_orchestrator.create_contract(
        state,
        parent_task_id='task-parent',
        parent_agent_id='AG-1',
        shade_agent_id='AG-2',
        conversation_id='conv-1',
        project='/tmp/proj',
        goal='Fix parser',
        phase_specs=[
            {'name': 'analysis', 'objective': 'Analyze'},
            {'name': 'implementation', 'objective': 'Implement'},
        ],
    )

    queue = [
        {
            'id': 'task-shade-1',
            'status': 'failed',
            'attempt_count': 2,
            'created_at': '2026-01-01T00:00:00+00:00',
            'started_at': '2026-01-01T00:00:01+00:00',
            'completed_at': '2026-01-01T00:00:02+00:00',
            'result_summary': 'compile error in parser',
            'shade_phase': {
                'contract_id': rec['id'],
                'phase_id': 'P02',
            },
        }
    ]
    (state / 'queue.json').write_text(json.dumps(queue, indent=2))

    attempts_dir = state / 'agents' / 'AG-2'
    attempts_dir.mkdir(parents=True, exist_ok=True)
    attempts_rows = [
        {'ts': '2026-01-01T00:00:01+00:00', 'task_id': 'task-shade-1', 'attempt_id': 'att-1', 'stage': 'attempt_started', 'payload': {}},
        {'ts': '2026-01-01T00:00:02+00:00', 'task_id': 'task-shade-1', 'attempt_id': 'att-1', 'stage': 'attempt_failed', 'payload': {'error': 'compile error'}},
    ]
    (attempts_dir / 'attempts.jsonl').write_text('\n'.join(json.dumps(r) for r in attempts_rows) + '\n')

    shade_orchestrator.mark_phase_failed(state, rec['id'], 'P02', task_id='task-shade-1', error='compile error in parser')

    buf = io.StringIO()
    with redirect_stdout(buf):
        charon_agents.cmd_shade_investigate(argparse.Namespace(contract_id=rec['id'], phase_id='P02', limit=50))

    out = json.loads(buf.getvalue())
    assert out['contract_id'] == rec['id']
    assert out['phase_id'] == 'P02'
    assert out['suggested_resume_phase_id'] == 'P02'
    assert 'compile error' in (out.get('failure_signature') or '')
