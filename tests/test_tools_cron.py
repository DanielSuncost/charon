

from charon import tools as tools_mod

from charon.tools import cron_tool as cron_mod


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
