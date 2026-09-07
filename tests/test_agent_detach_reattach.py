"""A persistent agent's tmux session really does survive detach and can be
found again later — not just that agent_lifecycle records a tmux_session
string.

Prime Agent markets daemon-backed continuity (a session survives terminal
detach and can be reattached later) as a headline feature. Charon's answer
predates this session and is simpler: a persistent agent already runs inside
a real tmux session (agent_lifecycle._ensure_tmux_session), which is the
standard, inspectable, battle-tested primitive for exactly this — `tmux
attach -t charon-<agent_id>` from any terminal, any time, no custom protocol
needed. This test exercises the real tmux session end to end (create,
detached by construction, alive and addressable "later") rather than trusting
that a recorded tmux_session field implies a live process.
"""
import shutil
import subprocess

import pytest

from charon.agents import agent_lifecycle

pytestmark = pytest.mark.skipif(shutil.which('tmux') is None, reason='tmux not available on PATH')


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Real create_agent()/load_agents() calls go through config.state_dir()
    (CHARON_STATE_DIR) rather than agent_lifecycle's own STATE_DIR fallback
    — set it so this test never touches the real global agent registry."""
    monkeypatch.setenv('CHARON_STATE_DIR', str(tmp_path))
    yield


def _has_session(name: str) -> bool:
    return subprocess.run(['tmux', 'has-session', '-t', name], capture_output=True).returncode == 0


def _kill_session(name: str) -> None:
    subprocess.run(['tmux', 'kill-session', '-t', name], capture_output=True)


def test_persistent_agent_tmux_session_survives_detach_and_is_reattachable(tmp_path):
    agent = agent_lifecycle.create_agent(
        'DetachDemo', 'persistent', 'demo detach/reattach', project=str(tmp_path), require_tmux=True,
    )
    session = agent['tmux_session']
    assert session == f"charon-{agent['id']}"

    try:
        # A session created with `tmux new-session -d` starts already
        # detached — there is no attached client, exactly the state a real
        # session is in the instant a user's terminal disconnects.
        assert _has_session(session), 'tmux session should exist and be running, detached, right after creation'

        # Simulate real work happening in the background while nobody is
        # attached: send a real command into the pane.
        marker = 'charon-detach-reattach-marker'
        subprocess.run(['tmux', 'send-keys', '-t', session, f'echo {marker}', 'Enter'], capture_output=True, check=True)

        # "Reattach later": a brand-new process re-reads the agent registry
        # from disk (not the in-memory `agent` object above) and finds the
        # same session still alive and addressable.
        reloaded = {a['id']: a for a in agent_lifecycle.load_agents()}[agent['id']]
        assert reloaded['tmux_session'] == session
        assert reloaded['status'] == 'running'
        assert _has_session(session), 'session must still be alive for a later reattach'

        # Prove the pane's real output is actually there to reattach to —
        # `tmux attach` puts a client on exactly this pane's history.
        captured = subprocess.run(['tmux', 'capture-pane', '-t', session, '-p'], capture_output=True, text=True, check=True)
        assert marker in captured.stdout
    finally:
        _kill_session(session)


def test_create_agent_without_require_tmux_has_no_session_to_reattach_to(tmp_path):
    """Contrast case: a temp/non-persistent agent makes no continuity claim
    — no tmux_session is recorded, and nothing should be treated as
    reattachable."""
    agent = agent_lifecycle.create_agent(
        'TempDemo', 'temp', 'one-shot task', project=str(tmp_path), require_tmux=False,
    )
    assert agent['tmux_session'] is None
