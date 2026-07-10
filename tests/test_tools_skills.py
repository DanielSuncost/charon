

from charon import tools as tools_mod

from charon.tools import skills_tool as sk_mod


def _ctx(tmp_path):
    proj = tmp_path / 'proj'
    proj.mkdir(parents=True, exist_ok=True)
    return tools_mod.ToolContext(project_root=proj, agent_id='AG-1', state_dir=tmp_path / 'state')


def test_skills_create_view_list_patch_delete(tmp_path):
    ctx = _ctx(tmp_path)
    assert not sk_mod.execute_skills({'action': 'create', 'name': 'demo-skill', 'content': 'hello world'}, ctx).is_error
    v = sk_mod.execute_skills({'action': 'view', 'name': 'demo-skill'}, ctx)
    assert 'hello world' in v.content
    assert 'demo-skill' in sk_mod.execute_skills({'action': 'list'}, ctx).content
    assert not sk_mod.execute_skills({'action': 'patch', 'name': 'demo-skill', 'old_string': 'world', 'new_string': 'charon'}, ctx).is_error
    v2 = sk_mod.execute_skills({'action': 'view', 'name': 'demo-skill'}, ctx)
    assert 'charon' in v2.content
    assert not sk_mod.execute_skills({'action': 'delete', 'name': 'demo-skill'}, ctx).is_error
