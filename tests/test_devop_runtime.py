from devop_runtime import (
    init_operation,
    get_operation_state,
    save_candidate_workstreams,
    init_workstream,
    get_workstream_state,
    save_evidence_bundle,
    save_checkpoint,
    save_review,
    select_best_checkpoint,
    finalize_operation_selection,
    append_handoff,
)


def test_devop_operation_and_workstream_lifecycle(tmp_path):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'project'
    project_root.mkdir()

    op = init_operation(
        state_dir,
        project_root,
        prompt='Build a web app that does X',
        title='Web app build',
        coordinator_agent_id='AG-COORD',
    )
    assert op['domain'] == 'software_dev'
    assert op['status'] == 'running'

    saved = save_candidate_workstreams(
        state_dir,
        op['operation_id'],
        [
            {'title': 'Frontend UI', 'summary': 'Build the UI'},
            {'title': 'Backend API', 'summary': 'Build the API'},
        ],
    )
    assert saved['count'] == 2

    ws = init_workstream(
        state_dir,
        op['operation_id'],
        title='Frontend UI',
        summary='Build the user-facing interface',
        acceptance_criteria=['Form renders', 'Basic validation works'],
        owner_agent_id='AG-FRONTEND',
        paired_judge_agent_id='AG-JUDGE',
    )
    assert ws['title'] == 'Frontend UI'
    assert ws['owner_agent_id'] == 'AG-FRONTEND'

    handoff = append_handoff(
        state_dir,
        op['operation_id'],
        workstream_id=ws['workstream_id'],
        kind='assignment',
        from_agent_id='AG-COORD',
        to_agent_id='AG-FRONTEND',
        from_role='coordinator',
        to_role='implementer',
        summary='Assigned frontend UI workstream',
    )
    assert handoff['kind'] == 'assignment'

    evidence = save_evidence_bundle(
        state_dir,
        op['operation_id'],
        ws['slug'],
        checkpoint_id='pending',
        changed_files=['src/ui/login.tsx', 'tests/login.test.tsx'],
        commands=['npm test -- login', 'npm run build'],
        verification={'tests_passed': 12, 'build_status': 'passed'},
        summary='Tests and build passed.',
    )
    assert evidence['changed_files'][0] == 'src/ui/login.tsx'

    checkpoint = save_checkpoint(
        state_dir,
        op['operation_id'],
        ws['slug'],
        producer_agent_id='AG-FRONTEND',
        markdown='# Frontend checkpoint\n\nAdded validation and tests.',
        summary='Added validation and tests',
        evidence_bundle_id=evidence['evidence_bundle_id'],
        scorecard={'overall': 0.80, 'requirements_fit': 0.84},
        best_so_far=True,
    )
    assert checkpoint['checkpoint_id'].startswith('cp-frontend-ui-')
    assert checkpoint['best_so_far'] is True

    review = save_review(
        state_dir,
        op['operation_id'],
        ws['slug'],
        checkpoint_id=checkpoint['checkpoint_id'],
        reviewer_agent_id='AG-JUDGE',
        review_type='judge',
        decision='repair_requested',
        critique_markdown='# Critique\n\nAdd more edge-case tests.',
        summary='Good progress, but edge cases are missing.',
        scores={'overall': 0.73, 'test_adequacy': 0.58},
        requested_changes=['Add malformed email tests'],
    )
    assert review['decision'] == 'repair_requested'

    ws_state = get_workstream_state(state_dir, op['operation_id'], ws['slug'])
    assert ws_state['status'] == 'revising'
    assert ws_state['latest_checkpoint']['checkpoint_id'] == checkpoint['checkpoint_id']
    assert ws_state['latest_review']['review_id'] == review['review_id']

    op_state = get_operation_state(state_dir, op['operation_id'])
    assert len(op_state['workstreams']) == 1
    assert any(e['kind'] == 'checkpoint_submitted' for e in op_state['events_tail'])


def test_best_checkpoint_and_final_selection(tmp_path):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'project'
    project_root.mkdir()
    op = init_operation(state_dir, project_root, prompt='Build app', coordinator_agent_id='AG-COORD')
    ws = init_workstream(
        state_dir,
        op['operation_id'],
        title='Backend API',
        owner_agent_id='AG-BACKEND',
        paired_judge_agent_id='AG-JUDGE',
    )

    save_checkpoint(
        state_dir,
        op['operation_id'],
        ws['slug'],
        producer_agent_id='AG-BACKEND',
        markdown='v1',
        summary='First backend checkpoint',
        scorecard={'overall': 0.61},
    )
    cp2 = save_checkpoint(
        state_dir,
        op['operation_id'],
        ws['slug'],
        producer_agent_id='AG-BACKEND',
        markdown='v2',
        summary='Second backend checkpoint',
        scorecard={'overall': 0.88},
    )

    best = select_best_checkpoint(state_dir, op['operation_id'], ws['slug'])
    assert best['checkpoint_id'] == cp2['checkpoint_id']

    final = finalize_operation_selection(state_dir, op['operation_id'], actor_agent_id='AG-COORD')
    assert final['operation_id'] == op['operation_id']
    assert final['selections'][0]['checkpoint_id'] == cp2['checkpoint_id']

    op_state = get_operation_state(state_dir, op['operation_id'])
    assert op_state['status'] == 'delivered'
    assert cp2['checkpoint_id'] in op_state['delivered_checkpoint_ids']
