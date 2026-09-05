from pathlib import Path

from charon.agents import specialists
from charon.workspace.overseer_skill import install_overseer_skill, OVERSEER_SKILL_MD, skill_path
from charon.tools import ToolContext
from charon.tools import skills_tool


def test_overseer_template_exists_with_charter():
    assert 'overseer' in specialists.list_templates()
    tpl = specialists.TEMPLATES['overseer']
    assert 'engineering manager' in tpl['specialization']
    for must in ('WorkDispatch', 'WorkEvidence', 'OverseerReport', 'never completion', 'permission prompt'):
        assert must in tpl['charter']


def test_overseer_skill_installs_and_is_listed_by_the_skills_tool(tmp_path):
    state = tmp_path / 'state'
    p = install_overseer_skill(state)
    assert p == skill_path(state) and p.read_text() == OVERSEER_SKILL_MD
    # idempotent + preserves user edits unless overwrite
    p.write_text('# edited')
    install_overseer_skill(state)
    assert p.read_text() == '# edited'
    install_overseer_skill(state, overwrite=True)
    assert 'The cycle' in p.read_text()
    ctx = ToolContext(project_root=tmp_path / 'proj', agent_id='AG-1', state_dir=state)
    (tmp_path / 'proj').mkdir()
    r = skills_tool.execute_skills({'action': 'list'}, ctx)
    assert not r.is_error and 'overseer' in r.content
