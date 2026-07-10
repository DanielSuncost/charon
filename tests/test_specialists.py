"""Long-lived specialist agents: user-assigned specialization + role charter
that persist, survive the auto-labeler, shape the system prompt, and accumulate
an attributed episodic track record via the wired task-completion pipeline."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture(autouse=True)
def _restore_polluted_modules():
    """These tests reload agent_lifecycle/specialists into sys.modules bound to a
    temp state dir. Restore the originals afterwards so later tests in the suite
    don't import a module pointing at a deleted tmp_path."""
    names = ('agent_lifecycle', 'specialists', 'soft_specialization')
    saved = {n: sys.modules.get(n) for n in names}
    try:
        yield
    finally:
        for n, mod in saved.items():
            if mod is not None:
                sys.modules[n] = mod
            else:
                sys.modules.pop(n, None)


def _fresh_lifecycle(tmp_path):
    """Load an isolated agent_lifecycle bound to a temp state dir."""
    spec = importlib.util.spec_from_file_location(
        'agent_lifecycle', ROOT / 'src' / 'charon' / 'agents' / 'agent_lifecycle.py')
    mod = importlib.util.module_from_spec(spec)
    sys.modules['agent_lifecycle'] = mod
    spec.loader.exec_module(mod)
    mod.STATE_DIR = tmp_path / 'state'
    mod.AGENTS_FILE = mod.STATE_DIR / 'agents.json'
    return mod


def _fresh_specialists(lifecycle):
    spec = importlib.util.spec_from_file_location(
        'specialists', ROOT / 'src' / 'charon' / 'agents' / 'specialists.py')
    mod = importlib.util.module_from_spec(spec)
    sys.modules['specialists'] = mod
    spec.loader.exec_module(mod)
    mod.agent_lifecycle = lifecycle
    return mod


def test_create_specialist_from_template(tmp_path, monkeypatch):
    monkeypatch.setenv('CHARON_NO_SQLITE', '1')
    lifecycle = _fresh_lifecycle(tmp_path)
    specialists = _fresh_specialists(lifecycle)

    a = specialists.create_specialist('release-engineer', project=str(tmp_path),
                                      require_tmux=False)
    assert a['mode'] == 'persistent'
    assert a['specialization'] == 'release engineer'
    assert a['specialization_locked'] is True
    assert 'rollback' in a['charter']
    # persisted, not just returned
    saved = json.loads(lifecycle.AGENTS_FILE.read_text())
    assert saved[0]['specialization'] == 'release engineer'


def test_create_specialist_custom_and_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv('CHARON_NO_SQLITE', '1')
    lifecycle = _fresh_lifecycle(tmp_path)
    specialists = _fresh_specialists(lifecycle)

    a = specialists.create_specialist(
        'custom', name='dbre', specialization='database reliability engineer',
        charter='You own schema migrations.', require_tmux=False)
    assert a['name'] == 'dbre'
    assert a['role'] == 'database-reliability-engineer'

    import pytest
    with pytest.raises(ValueError):
        specialists.create_specialist('nonexistent-template', require_tmux=False)
    with pytest.raises(ValueError):
        specialists.create_specialist('custom', require_tmux=False)  # no specialization


def test_assign_specialization_locks_and_clears(tmp_path, monkeypatch):
    monkeypatch.setenv('CHARON_NO_SQLITE', '1')
    lifecycle = _fresh_lifecycle(tmp_path)
    a = lifecycle.create_agent(name='x', mode='persistent', goal='g',
                               project=str(tmp_path), require_tmux=False)

    out = lifecycle.assign_specialization(a['id'], 'security engineer',
                                          charter='You own security review.')
    assert out['specialization'] == 'security engineer'
    assert out['specialization_locked'] is True
    assert out['charter'] == 'You own security review.'

    cleared = lifecycle.assign_specialization(a['id'], '', charter='')
    assert 'specialization' not in cleared and 'charter' not in cleared

    assert lifecycle.assign_specialization('AG-9999', 'x') is None


def test_soft_specialization_respects_lock(tmp_path, monkeypatch):
    monkeypatch.setenv('CHARON_NO_SQLITE', '1')
    lifecycle = _fresh_lifecycle(tmp_path)
    a = lifecycle.create_agent(name='x', mode='persistent', goal='g',
                               project=str(tmp_path), require_tmux=False,
                               specialization='release engineer', charter='c')

    from charon.agents import soft_specialization as ss
    ss._last_refresh.clear()
    # would normally derive a label; the lock must short-circuit first
    label = ss.refresh_specialization(lifecycle.STATE_DIR, a['id'])
    assert label is None
    # and the stored value is untouched
    saved = json.loads(lifecycle.AGENTS_FILE.read_text())
    assert saved[0]['specialization'] == 'release engineer'


def test_charter_appears_in_system_prompt():
    from charon.context import system_prompt_builder as spb
    agent = {'id': 'AG-0042', 'name': 'rel-01', 'specialization': 'release engineer',
             'charter': 'You own releases end to end: never ship from a dirty tree.'}
    identity = spb._build_identity(agent, {})
    assert 'Role: release engineer' in identity
    assert '# Role charter' in identity
    assert 'never ship from a dirty tree' in identity
    # no charter -> no empty charter section
    assert '# Role charter' not in spb._build_identity({'name': 'x'}, {})


def test_task_promotion_creates_attributed_episode(tmp_path):
    """The wired pipeline: a completed daemon task becomes an Episode with typed
    events and the agent's id as source_agent — the WHO for cross-agent threads."""
    from charon.agents import agent_runtime
    from charon.memory.memory_engine import MemoryEngine
    from charon.memory import episodic as ep
    from charon.agents import threads as th

    state = tmp_path / 'state'
    state.mkdir()
    proj = str(tmp_path / 'proj')
    agent = {'id': 'AG-REL-1', 'name': 'rel-01', 'project': proj}
    task = {'id': 'task-77', 'project': proj}

    agent_runtime._promote_task_to_episode(
        state, agent, task,
        task_id='task-77',
        instruction='cut the v2.3 release',
        summary='Released v2.3 after verifying the suite.',
        tool_calls=[{'tool': 'Bash'}, {'tool': 'Git'}],
        response_text='Suite green. We decided to use annotated tags because they carry the changelog.',
        total_turns=3,
    )

    eng = MemoryEngine(state)
    tag = f'project:{Path(proj).resolve()}'
    eps = ep.list_episodes(eng, tag)
    assert len(eps) == 1
    assert eps[0].source_agent == 'AG-REL-1'
    types = [e.event_type for e in ep.get_events(eng, eps[0].id)]
    assert 'user_message' in types and types.count('tool_call') == 2
    # auto-captured decision is attributed to this agent in the thread layer
    w = th.why(eng, 'how do we tag releases', container_tag=tag)
    assert w and w[0]['agent'] == 'AG-REL-1'
    assert 'changelog' in w[0]['why']
    eng.close()


def test_promotion_never_raises(tmp_path):
    from charon.agents import agent_runtime
    # bogus everything — must swallow, task completion cannot break
    agent_runtime._promote_task_to_episode(
        Path('/nonexistent/dir'), {}, {},
        task_id='', instruction='', summary='', tool_calls=None,
        response_text='', total_turns=0)
