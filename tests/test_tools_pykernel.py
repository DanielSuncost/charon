from charon import tools as tools_mod
from charon.tools import pykernel_tool as pk_mod


def _ctx(tmp_path, agent_id='AG-PK-1'):
    proj = tmp_path / 'proj'
    proj.mkdir(parents=True, exist_ok=True)
    return tools_mod.ToolContext(project_root=proj, agent_id=agent_id, state_dir=tmp_path / 'state')


def teardown_function(_fn):
    # Kernels are real subprocesses keyed by agent_id — stop everything each test starts.
    with pk_mod._kernels_lock:
        workers = list(pk_mod._kernels.values())
        pk_mod._kernels.clear()
    for w in workers:
        w.close()


def test_pykernel_state_persists_across_calls(tmp_path):
    ctx = _ctx(tmp_path)
    r1 = pk_mod.execute_pykernel({'code': 'x = 21'}, ctx)
    assert not r1.is_error
    r2 = pk_mod.execute_pykernel({'code': 'x * 2'}, ctx)
    assert not r2.is_error
    assert 'Out: 42' in r2.content


def test_pykernel_stdout_captured(tmp_path):
    ctx = _ctx(tmp_path)
    r = pk_mod.execute_pykernel({'code': 'print("hello from kernel")'}, ctx)
    assert not r.is_error
    assert 'hello from kernel' in r.content


def test_pykernel_error_surfaced_without_crashing_kernel(tmp_path):
    ctx = _ctx(tmp_path)
    r1 = pk_mod.execute_pykernel({'code': 'raise ValueError("boom")'}, ctx)
    assert r1.is_error
    assert 'boom' in r1.content
    # An exception in one call must not kill the kernel or its prior state.
    r2 = pk_mod.execute_pykernel({'code': 'y = 5\ny'}, ctx)
    assert not r2.is_error
    assert 'Out: 5' in r2.content


def test_pykernel_reset_clears_state(tmp_path):
    ctx = _ctx(tmp_path)
    pk_mod.execute_pykernel({'code': 'z = 99'}, ctx)
    reset = pk_mod.execute_pykernel({'action': 'reset', 'code': ''}, ctx)
    assert not reset.is_error
    r = pk_mod.execute_pykernel({'code': 'z'}, ctx)
    assert r.is_error
    assert 'NameError' in r.content


def test_pykernel_status_reports_alive(tmp_path):
    ctx = _ctx(tmp_path)
    before = pk_mod.execute_pykernel({'action': 'status', 'code': ''}, ctx)
    assert before.details == {'alive': False}
    pk_mod.execute_pykernel({'code': '1'}, ctx)
    after = pk_mod.execute_pykernel({'action': 'status', 'code': ''}, ctx)
    assert after.details['alive'] is True
    assert after.details['calls'] >= 1


def test_pykernel_two_agents_get_isolated_kernels(tmp_path):
    ctx_a = _ctx(tmp_path, agent_id='AG-A')
    ctx_b = _ctx(tmp_path, agent_id='AG-B')
    pk_mod.execute_pykernel({'code': 'shared = "a"'}, ctx_a)
    pk_mod.execute_pykernel({'code': 'shared = "b"'}, ctx_b)
    ra = pk_mod.execute_pykernel({'code': 'shared'}, ctx_a)
    rb = pk_mod.execute_pykernel({'code': 'shared'}, ctx_b)
    assert "Out: 'a'" in ra.content
    assert "Out: 'b'" in rb.content


def test_pykernel_bridge_exposes_spawn_shade(tmp_path):
    ctx = _ctx(tmp_path)
    r = pk_mod.execute_pykernel({'code': 'callable(charon.spawn_shade)'}, ctx)
    assert not r.is_error
    assert 'Out: True' in r.content


def test_pykernel_timeout_soft_interrupts_and_preserves_state(tmp_path):
    ctx = _ctx(tmp_path)
    r = pk_mod.execute_pykernel(
        {'code': 'kept = "before"\nimport time\ntime.sleep(5)', 'timeout_sec': 1}, ctx,
    )
    assert r.is_error
    assert 'interrupted' in r.content.lower()
    # A timeout must not destroy state the model built up before it —
    # variables assigned before the interrupted statement must survive.
    r2 = pk_mod.execute_pykernel({'code': 'kept'}, ctx)
    assert not r2.is_error
    assert "Out: 'before'" in r2.content
