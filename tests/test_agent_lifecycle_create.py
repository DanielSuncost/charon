from pathlib import Path
import importlib.util
import pytest
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'apps' / 'core-daemon' / 'agent_lifecycle.py'

spec = importlib.util.spec_from_file_location('agent_lifecycle_create', MODULE_PATH)
agent_lifecycle = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = agent_lifecycle
spec.loader.exec_module(agent_lifecycle)


def test_create_agent_fails_fast_when_tmux_creation_fails(tmp_path, monkeypatch):
    agent_lifecycle.STATE_DIR = tmp_path / 'state'
    agent_lifecycle.AGENTS_FILE = agent_lifecycle.STATE_DIR / 'agents.json'
    agent_lifecycle.INTERVENTIONS_FILE = agent_lifecycle.STATE_DIR / 'interventions.jsonl'

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


def test_create_agent_autonames_charon_per_project_without_tmux(tmp_path):
    agent_lifecycle.STATE_DIR = tmp_path / 'state'
    agent_lifecycle.AGENTS_FILE = agent_lifecycle.STATE_DIR / 'agents.json'
    agent_lifecycle.INTERVENTIONS_FILE = agent_lifecycle.STATE_DIR / 'interventions.jsonl'

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
