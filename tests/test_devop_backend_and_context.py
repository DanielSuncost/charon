import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))
sys.path.insert(0, str(ROOT / 'apps' / 'tui' / 'opentui'))

from conversation_engine import ConversationEngine
from providers import ModelInfo
from devop_runtime import init_operation, init_workstream, save_checkpoint, save_review
from chat_backend import _collect_devop_rooms


class DummyProvider:
    async def stream(self, *args, **kwargs):
        if False:
            yield None


def test_conversation_engine_tool_context_carries_operation_metadata(tmp_path):
    engine = ConversationEngine(
        provider=DummyProvider(),
        model=ModelInfo(provider='dummy', model_id='dummy-model'),
        project_root=tmp_path,
        state_dir=tmp_path / 'state',
        agent_id='AG-DEV',
        operation_id='op-dev-123',
        operation_domain='software_dev',
        work_unit_id='frontend-ui',
        operation_role='implementer',
        runtime_role='background_agent',
        parent_agent_id='AG-COORD',
        auto_compact=False,
    )

    ctx = engine.tool_context
    assert ctx.operation_id == 'op-dev-123'
    assert ctx.operation_domain == 'software_dev'
    assert ctx.work_unit_id == 'frontend-ui'
    assert ctx.operation_role == 'implementer'
    assert ctx.runtime_role == 'background_agent'
    assert ctx.parent_agent_id == 'AG-COORD'


def test_collect_devop_rooms_from_refresh_projection(tmp_path):
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
        scorecard={'overall': 0.91},
    )
    save_review(
        state_dir,
        op['operation_id'],
        ws['slug'],
        checkpoint_id=cp['checkpoint_id'],
        reviewer_agent_id='AG-JUDGE',
        review_type='judge',
        decision='accepted',
        critique_markdown='looks good',
        summary='Ready',
        scores={'overall': 0.89},
    )

    rooms = _collect_devop_rooms(state_dir, project_root)
    assert len(rooms) == 1
    room = rooms[0]
    assert room['kind'] == 'software_dev'
    assert room['operation_id'] == op['operation_id']
    assert any(p['role'] == 'implementer' for p in room['participants'])
    assert any(w['slug'] == ws['slug'] for w in room['workstreams'])
    assert len(room['events']) >= 1
