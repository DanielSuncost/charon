from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'apps' / 'core-daemon'
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

TOOLS_PATH = ROOT / 'apps' / 'core-daemon' / 'tools' / '__init__.py'
CRON_PATH = ROOT / 'apps' / 'core-daemon' / 'tools' / 'cron_tool.py'

spec_tools = importlib.util.spec_from_file_location('tools', TOOLS_PATH)
tools_mod = importlib.util.module_from_spec(spec_tools)
sys.modules['tools'] = tools_mod
spec_tools.loader.exec_module(tools_mod)

spec_cron = importlib.util.spec_from_file_location('cron_tool', CRON_PATH)
cron_mod = importlib.util.module_from_spec(spec_cron)
sys.modules['cron_tool'] = cron_mod
spec_cron.loader.exec_module(cron_mod)


def _ctx(tmp_path):
    proj = tmp_path / 'proj'
    proj.mkdir(parents=True, exist_ok=True)
    return tools_mod.ToolContext(project_root=proj, agent_id='AG-1', state_dir=tmp_path / 'state')


def test_cron_create_and_list(tmp_path):
    ctx = _ctx(tmp_path)
    r = cron_mod.execute_cron({'action': 'create', 'prompt': 'do x', 'schedule': 'every 2h'}, ctx)
    assert not r.is_error

    r2 = cron_mod.execute_cron({'action': 'list'}, ctx)
    assert not r2.is_error
    assert 'Cron jobs (1)' in r2.content


def test_cron_pause_resume_run_remove(tmp_path):
    ctx = _ctx(tmp_path)
    r = cron_mod.execute_cron({'action': 'create', 'prompt': 'do y', 'schedule': '30m', 'name': 'demo'}, ctx)
    assert not r.is_error
    job_id = r.details['job']['job_id']

    p = cron_mod.execute_cron({'action': 'pause', 'job_id': job_id, 'reason': 'maintenance'}, ctx)
    assert not p.is_error

    rr = cron_mod.execute_cron({'action': 'resume', 'job_id': job_id}, ctx)
    assert not rr.is_error

    rn = cron_mod.execute_cron({'action': 'run', 'job_id': job_id}, ctx)
    assert not rn.is_error

    rm = cron_mod.execute_cron({'action': 'remove', 'job_id': job_id}, ctx)
    assert not rm.is_error

    ls = cron_mod.execute_cron({'action': 'list', 'include_disabled': True}, ctx)
    assert job_id in ls.content
