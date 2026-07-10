

from charon.shade import shade_orchestrator


def test_contract_create_and_branch_from_phase(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)

    rec = shade_orchestrator.create_contract(
        state,
        parent_task_id='task-1',
        parent_agent_id='AG-1',
        shade_agent_id='AG-2',
        conversation_id='conv-1',
        project='/tmp/proj',
        goal='Build feature X',
        constraints=['Do not edit migrations', 'Keep API stable'],
        expected_outputs=['Updated handler', 'Tests passing'],
        scope=['src/api'],
    )

    assert rec['id'].startswith('ctr-')
    assert rec['phases'][0]['phase_id'] == 'P01'
    assert rec['phases'][0]['lookup_key'].startswith(rec['id'] + ':')

    shade_orchestrator.mark_phase_completed(state, rec['id'], 'P01', task_id='task-a', summary='planned')
    shade_orchestrator.mark_phase_failed(state, rec['id'], 'P02', task_id='task-b', error='boom')

    branched = shade_orchestrator.branch_from_phase(
        state,
        contract_id=rec['id'],
        from_phase_id='P02',
        reason='retry from implementation',
    )
    assert branched is not None
    assert branched['active_branch_id'] == 'b01'
    p1, p2 = branched['phases'][0], branched['phases'][1]
    assert p1['status'] == 'completed'
    assert p2['status'] == 'pending'

    events = shade_orchestrator.load_phase_events(state, contract_id=rec['id'])
    kinds = [e['event_type'] for e in events]
    assert 'contract_created' in kinds
    assert 'phase_failed' in kinds
    assert 'contract_branched' in kinds


def test_suggest_branch_phase_prefers_failure_then_current(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)

    rec = shade_orchestrator.create_contract(
        state,
        parent_task_id='task-2',
        parent_agent_id='AG-1',
        shade_agent_id='AG-2',
        conversation_id='conv-2',
        project='/tmp/proj',
        goal='Build feature Y',
        phase_specs=[
            {'name': 'analysis', 'objective': 'Analyze'},
            {'name': 'implementation', 'objective': 'Implement'},
            {'name': 'verify', 'objective': 'Verify'},
        ],
    )
    shade_orchestrator.mark_phase_completed(state, rec['id'], 'P01', task_id='t1', summary='done')
    shade_orchestrator.mark_phase_failed(state, rec['id'], 'P02', task_id='t2', error='compile error')
    latest = shade_orchestrator.get_contract(state, rec['id'])

    guess = shade_orchestrator.suggest_branch_phase(latest)
    assert guess['recommended_phase_id'] == 'P02'
    assert 'failed' in guess['reason'].lower()

    guess2 = shade_orchestrator.suggest_branch_phase(latest, phase_id='P01')
    assert guess2['recommended_phase_id'] == 'P02'
