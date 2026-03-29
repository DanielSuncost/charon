from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
TOOLS_PATH = ROOT / 'apps' / 'core-daemon' / 'tools' / '__init__.py'
SKILLS_PATH = ROOT / 'apps' / 'core-daemon' / 'tools' / 'skills_tool.py'

spec_tools = importlib.util.spec_from_file_location('tools', TOOLS_PATH)
tools_mod = importlib.util.module_from_spec(spec_tools)
sys.modules['tools'] = tools_mod
spec_tools.loader.exec_module(tools_mod)

spec_sk = importlib.util.spec_from_file_location('skills_tool', SKILLS_PATH)
sk_mod = importlib.util.module_from_spec(spec_sk)
sys.modules['skills_tool'] = sk_mod
spec_sk.loader.exec_module(sk_mod)


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
