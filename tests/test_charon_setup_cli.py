from pathlib import Path
import argparse
import importlib.util
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / 'scripts' / 'charon_agents.py'

spec_cli = importlib.util.spec_from_file_location('charon_agents_setup_cli_test', SCRIPT_PATH)
charon_agents = importlib.util.module_from_spec(spec_cli)
sys.modules[spec_cli.name] = charon_agents
spec_cli.loader.exec_module(charon_agents)


def test_setup_provider_model_complete_updates_onboarding_state(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    charon_agents.STATE_DIR = state

    charon_agents.cmd_setup_provider(argparse.Namespace(name='opencode'))
    charon_agents.cmd_setup_model(argparse.Namespace(name='lmstudio/qwen-test'))
    charon_agents.cmd_setup_project(argparse.Namespace(name='charon'))
    charon_agents.cmd_setup_complete(argparse.Namespace())

    onboarding = json.loads((state / 'onboarding.json').read_text())
    assert onboarding['provider_mode'] == 'provider'
    assert onboarding['provider'] == 'opencode'
    assert onboarding['model'] == 'lmstudio/qwen-test'
    assert onboarding['complete'] is True


def test_setup_api_key_writes_auth_store(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    charon_agents.STATE_DIR = state

    charon_agents.cmd_setup_api_key(argparse.Namespace(key='sk-or-test'))

    auth = json.loads((state / 'auth' / 'auth.json').read_text())
    assert auth['active_provider'] == 'openrouter'
    assert auth['providers']['openrouter']['api_key'] == 'sk-or-test'

    onboarding = json.loads((state / 'onboarding.json').read_text())
    assert onboarding['provider_mode'] == 'provider'
    assert onboarding['provider'] == 'api'
