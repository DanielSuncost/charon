from pathlib import Path
import importlib.util
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
CORE_DAEMON = ROOT / 'apps' / 'core-daemon'
if str(CORE_DAEMON) not in sys.path:
    sys.path.insert(0, str(CORE_DAEMON))

MOD_PATH = ROOT / 'apps' / 'core-daemon' / 'shade_orchestrator.py'

spec = importlib.util.spec_from_file_location('shade_orchestrator', MOD_PATH)
shade_orchestrator = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = shade_orchestrator
spec.loader.exec_module(shade_orchestrator)


def test_assess_contract_outcome_flags_weak_completed_output(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)

    rec = shade_orchestrator.create_contract(
        state,
        parent_task_id='task-1',
        parent_agent_id='AG-1',
        shade_agent_id='AG-2',
        conversation_id='conv-1',
        project='/tmp/proj',
        goal='Implement fix',
        expected_outputs=['Code changes', 'Tests passing', 'Report'],
    )

    shade_orchestrator.mark_phase_completed(state, rec['id'], 'P01', task_id='task-a', summary='\n\n\n')
    contract = shade_orchestrator.get_contract(state, rec['id'])
    assessment = shade_orchestrator.assess_contract_outcome(contract)

    assert assessment['outcome'] == 'partial'
    assert assessment['quality_flags']
    assert assessment['quality_flags'][0]['reason'] == 'weak_summary'

    events = shade_orchestrator.load_phase_events(state, contract_id=rec['id'])
    assert any(e['event_type'] == 'phase_output_suspect' for e in events)


def test_assess_contract_outcome_marks_failed_quality_when_all_phases_complete_but_weak(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)

    rec = shade_orchestrator.create_contract(
        state,
        parent_task_id='task-2',
        parent_agent_id='AG-1',
        shade_agent_id='AG-2',
        conversation_id='conv-2',
        project='/tmp/proj',
        goal='Implement fix',
    )

    shade_orchestrator.mark_phase_completed(state, rec['id'], 'P01', task_id='t1', summary='good analysis with actual detail')
    shade_orchestrator.mark_phase_completed(state, rec['id'], 'P02', task_id='t2', summary='implemented changes in target files')
    shade_orchestrator.mark_phase_completed(state, rec['id'], 'P03', task_id='t3', summary='verified with tests passing locally')
    shade_orchestrator.mark_phase_completed(state, rec['id'], 'P04', task_id='t4', summary='\n\n')

    contract = shade_orchestrator.get_contract(state, rec['id'])
    assessment = shade_orchestrator.assess_contract_outcome(contract)
    assert assessment['outcome'] == 'failed_quality'
    assert any(flag['phase_id'] == 'P04' for flag in assessment['quality_flags'])


def test_save_triage_record_persists_recommendation(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)

    rec = shade_orchestrator.create_contract(
        state,
        parent_task_id='task-3',
        parent_agent_id='AG-1',
        shade_agent_id='AG-2',
        conversation_id='conv-3',
        project='/tmp/proj',
        goal='Implement fix',
        expected_outputs=['code', 'tests', 'report'],
    )
    shade_orchestrator.mark_phase_completed(state, rec['id'], 'P01', task_id='t1', summary='\n')
    contract = shade_orchestrator.get_contract(state, rec['id'])
    assessment = shade_orchestrator.assess_contract_outcome(contract)
    triage = shade_orchestrator.save_triage_record(state, contract, assessment)

    assert triage['assessment']['outcome'] == 'partial'
    assert triage['recommendation'] == 'inspect_stall_and_resume_or_escalate'

    events = shade_orchestrator.load_phase_events(state, contract_id=rec['id'])
    assert any(e['event_type'] == 'worker_triage_requested' for e in events)

    triage_path = state / 'shade_triage.json'
    data = json.loads(triage_path.read_text())
    assert data['items']
    assert data['items'][0]['recommendation'] == 'inspect_stall_and_resume_or_escalate'
