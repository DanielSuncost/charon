

from charon import tools as tools_mod

from charon.tools import execute_code_tool as ec_mod


def _ctx(tmp_path):
    proj = tmp_path / 'proj'
    proj.mkdir(parents=True, exist_ok=True)
    return tools_mod.ToolContext(project_root=proj, agent_id='AG-1', state_dir=tmp_path / 'state')


def test_execute_code_happy_path(tmp_path):
    ctx = _ctx(tmp_path)
    r = ec_mod.execute_execute_code({'code': 'print(1+2)'}, ctx)
    assert not r.is_error
    assert '3' in r.content
