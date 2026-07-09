from devop_runtime import init_operation, init_workstream, save_checkpoint, save_review, append_decision
from devop_projection import project_graph, project_room_messages, project_f4_stream, summarize_operation, summarize_workstream


def test_devop_projection_outputs(tmp_path):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'project'
    project_root.mkdir()

    op = init_operation(state_dir, project_root, prompt='Build a web app', coordinator_agent_id='AG-COORD')
    ws = init_workstream(
        state_dir,
        op['operation_id'],
        title='Frontend UI',
        owner_agent_id='AG-FRONTEND',
        paired_judge_agent_id='AG-JUDGE',
    )
    cp = save_checkpoint(
        state_dir,
        op['operation_id'],
        ws['slug'],
        producer_agent_id='AG-FRONTEND',
        markdown='checkpoint',
        summary='Frontend checkpoint',
        scorecard={'overall': 0.81},
    )
    rv = save_review(
        state_dir,
        op['operation_id'],
        ws['slug'],
        checkpoint_id=cp['checkpoint_id'],
        reviewer_agent_id='AG-JUDGE',
        review_type='judge',
        decision='repair_requested',
        critique_markdown='critique',
        summary='Need better tests',
        scores={'overall': 0.72},
    )
    append_decision(
        state_dir,
        op['operation_id'],
        decision_type='select_best_checkpoint',
        actor_agent_id='AG-COORD',
        workstream_id=ws['workstream_id'],
        subject_id=cp['checkpoint_id'],
        summary='Selected current checkpoint as best-so-far',
    )

    graph = project_graph(state_dir, op['operation_id'])
    node_ids = {n['id'] for n in graph['nodes']}
    assert op['operation_id'] in node_ids
    assert ws['workstream_id'] in node_ids
    assert cp['checkpoint_id'] in node_ids
    assert rv['review_id'] in node_ids
    assert any(e['edge_type'] == 'submits' for e in graph['edges'])
    assert any(e['edge_type'] == 'reviews' for e in graph['edges'])

    rooms = project_room_messages(state_dir, op['operation_id'])
    assert any(m['message_class'] == 'checkpoint_notice' for m in rooms)
    assert any(m['message_class'] == 'review_notice' for m in rooms)

    f4 = project_f4_stream(state_dir, op['operation_id'])
    assert f4['operation_id'] == op['operation_id']
    assert any(item['item_class'] == 'checkpoint_item' for item in f4['stream'])
    assert any(item['item_class'] == 'review_item' for item in f4['stream'])
    assert any(w['slug'] == ws['slug'] for w in f4['workstreams'])

    op_summary = summarize_operation(state_dir, op['operation_id'])
    assert op_summary['workstream_count'] == 1
    assert op_summary['checkpoint_count'] == 1
    assert op_summary['review_count'] == 1

    ws_summary = summarize_workstream(state_dir, op['operation_id'], ws['slug'])
    assert ws_summary['slug'] == ws['slug']
    assert ws_summary['checkpoint_id'] == cp['checkpoint_id']
