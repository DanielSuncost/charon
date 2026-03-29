from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
TOOLS_PATH = ROOT / 'apps' / 'core-daemon' / 'tools' / '__init__.py'
EC_PATH = ROOT / 'apps' / 'core-daemon' / 'tools' / 'execute_code_tool.py'

spec_tools = importlib.util.spec_from_file_location('tools', TOOLS_PATH)
tools_mod = importlib.util.module_from_spec(spec_tools)
sys.modules['tools'] = tools_mod
spec_tools.loader.exec_module(tools_mod)

spec_ec = importlib.util.spec_from_file_location('execute_code_tool', EC_PATH)
ec_mod = importlib.util.module_from_spec(spec_ec)
sys.modules['execute_code_tool'] = ec_mod
spec_ec.loader.exec_module(ec_mod)


def _ctx(tmp_path):
    proj = tmp_path / 'proj'
    proj.mkdir(parents=True, exist_ok=True)
    return tools_mod.ToolContext(project_root=proj, agent_id='AG-1', state_dir=tmp_path / 'state')


def test_execute_code_happy_path(tmp_path):
    ctx = _ctx(tmp_path)
    r = ec_mod.execute_execute_code({'code': 'print(1+2)'}, ctx)
    assert not r.is_error
    assert '3' in r.content
