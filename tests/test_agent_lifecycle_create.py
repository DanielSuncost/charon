import pytest


from charon.agents import agent_lifecycle


def test_create_agent_fails_fast_when_tmux_creation_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_lifecycle, 'STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr(agent_lifecycle, 'AGENTS_FILE', tmp_path / 'state' / 'agents.json')
    monkeypatch.setattr(agent_lifecycle, 'INTERVENTIONS_FILE', tmp_path / 'state' / 'interventions.jsonl')

    monkeypatch.setattr(agent_lifecycle, '_ensure_tmux_session', lambda *_args, **_kwargs: (False, 'tmux boom'))

    with pytest.raises(RuntimeError):
        agent_lifecycle.create_agent(
            name='charon-demo-01',
            mode='persistent',
            goal='test',
            project=str(tmp_path / 'proj'),
            require_tmux=True,
        )

    assert not agent_lifecycle.AGENTS_FILE.exists()


def test_create_agent_autonames_charon_per_project_without_tmux(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_lifecycle, 'STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr(agent_lifecycle, 'AGENTS_FILE', tmp_path / 'state' / 'agents.json')
    monkeypatch.setattr(agent_lifecycle, 'INTERVENTIONS_FILE', tmp_path / 'state' / 'interventions.jsonl')

    a1 = agent_lifecycle.create_agent(
        name='',
        mode='persistent',
        goal='demo',
        project='/tmp/my-project',
        require_tmux=False,
    )
    a2 = agent_lifecycle.create_agent(
        name='',
        mode='persistent',
        goal='demo',
        project='/tmp/my-project',
        require_tmux=False,
    )

    assert a1['name'] == 'charon-my-project-01'
    assert a2['name'] == 'charon-my-project-02'
    assert a1['tmux_session'] is None
    assert a1['role'] == 'charon'
    assert a1['visibility'] == 'user'
