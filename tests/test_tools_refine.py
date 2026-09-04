from charon import tools as tools_mod
from charon.judge.judge_engine import load_loop
from charon.tools import refine_tool as rf_mod


def _ctx(tmp_path):
    proj = tmp_path / 'proj'
    proj.mkdir(parents=True, exist_ok=True)
    return tools_mod.ToolContext(project_root=proj, agent_id='AG-1', state_dir=tmp_path / 'state')


def test_refine_propose_requires_evidence(tmp_path):
    ctx = _ctx(tmp_path)
    r = rf_mod.execute_refine(
        {'action': 'propose', 'skill': 'demo', 'proposed_change': 'add a note'}, ctx,
    )
    assert r.is_error
    assert 'evidence' in r.content.lower()


def test_refine_propose_requires_proposed_change(tmp_path):
    ctx = _ctx(tmp_path)
    r = rf_mod.execute_refine(
        {'action': 'propose', 'skill': 'demo', 'evidence': ['user hit X twice']}, ctx,
    )
    assert r.is_error
    assert 'proposed_change' in r.content.lower()


def test_refine_invalid_skill_name_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    r = rf_mod.execute_refine(
        {'action': 'propose', 'skill': 'Not Valid!', 'evidence': ['e'], 'proposed_change': 'c'}, ctx,
    )
    assert r.is_error


def test_refine_propose_creates_loop_and_record(tmp_path):
    ctx = _ctx(tmp_path)
    r = rf_mod.execute_refine(
        {
            'action': 'propose',
            'skill': 'demo-skill',
            'evidence': ['agent forgot to check X in trajectory Y'],
            'proposed_change': 'note that X must be checked first',
        },
        ctx,
    )
    assert not r.is_error
    refinement_id = r.details['refinement_id']
    loop_id = r.details['loop_id']
    assert refinement_id.startswith('refine-')

    loop = load_loop(ctx.state_dir, loop_id)
    assert loop is not None
    assert loop.judge_type == 'aesthetic'
    assert loop.scope == ['SKILL.md']
    assert 'agent forgot to check X' in loop.rubric

    skill_file = ctx.state_dir / 'skills' / 'demo-skill' / 'SKILL.md'
    assert skill_file.exists()


def test_refine_status_and_list(tmp_path):
    ctx = _ctx(tmp_path)
    r = rf_mod.execute_refine(
        {
            'action': 'propose',
            'skill': 'other-skill',
            'evidence': ['evidence line'],
            'proposed_change': 'change it',
        },
        ctx,
    )
    refinement_id = r.details['refinement_id']

    status = rf_mod.execute_refine({'action': 'status', 'refinement_id': refinement_id}, ctx)
    assert not status.is_error
    assert 'other-skill' in status.content

    listing = rf_mod.execute_refine({'action': 'list'}, ctx)
    assert not listing.is_error
    assert refinement_id in listing.content
