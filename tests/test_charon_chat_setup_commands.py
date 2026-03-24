from pathlib import Path
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / 'scripts' / 'charon_agents.py'

spec_cli = importlib.util.spec_from_file_location('charon_agents_chat_setup_test', SCRIPT_PATH)
charon_agents = importlib.util.module_from_spec(spec_cli)
sys.modules[spec_cli.name] = charon_agents
spec_cli.loader.exec_module(charon_agents)


def test_chat_setup_provider_and_model_commands_update_onboarding(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    charon_agents.STATE_DIR = state

    out = io.StringIO()
    with redirect_stdout(out):
        assert charon_agents._handle_chat_slash_command('/setup provider claude-code', agent_id='AG-1', conversation_id='conv-1', session_id='', project='', limit=20)
        assert charon_agents._handle_chat_slash_command('/model claude-3-7-sonnet', agent_id='AG-1', conversation_id='conv-1', session_id='', project='', limit=20)

    onboarding = json.loads((state / 'onboarding.json').read_text())
    assert onboarding['provider'] == 'claude-code'
    assert onboarding['provider_mode'] == 'provider'
    assert onboarding['model'] == 'claude-3-7-sonnet'


def test_chat_setup_suggestions_render(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    charon_agents.STATE_DIR = state

    out = io.StringIO()
    with redirect_stdout(out):
        assert charon_agents._handle_chat_slash_command('/setup', agent_id='AG-1', conversation_id='conv-1', session_id='', project='', limit=20)

    rendered = out.getvalue()
    assert 'Suggestions:' in rendered
    assert '/setup provider claude-code' in rendered


def test_chat_setup_typo_still_shows_provider_suggestion(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    charon_agents.STATE_DIR = state

    out = io.StringIO()
    with redirect_stdout(out):
        assert charon_agents._handle_chat_slash_command('/setup provier claude-code', agent_id='AG-1', conversation_id='conv-1', session_id='', project='', limit=20)

    rendered = out.getvalue()
    assert 'Suggestions:' in rendered
    assert '/setup provider claude-code' in rendered


def test_chat_setup_provider_claude_auto_starts_auth(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    charon_agents.STATE_DIR = state

    calls = {'count': 0}

    def _fake_auth_start(args):
        calls['count'] += 1

    monkeypatch.setattr(charon_agents, 'cmd_setup_auth_start', _fake_auth_start)

    class _TTY:
        @staticmethod
        def isatty():
            return True

    monkeypatch.setattr(charon_agents.sys, 'stdin', _TTY())

    assert charon_agents._handle_chat_slash_command(
        '/setup provider claude-code',
        agent_id='AG-1',
        conversation_id='conv-1',
        session_id='',
        project='',
        limit=20,
    )
    assert calls['count'] == 1
