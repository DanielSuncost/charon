from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'apps' / 'tui' / 'onboarding_state.py'

spec = importlib.util.spec_from_file_location('onboarding_state', MODULE_PATH)
onboarding_state = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = onboarding_state
spec.loader.exec_module(onboarding_state)


def test_default_onboarding_incomplete():
    state = onboarding_state.default_onboarding()
    assert state['complete'] is False
    assert state['step'] == 'provider-mode'


def test_load_onboarding_falls_back_to_default(tmp_path):
    state = onboarding_state.load_onboarding(tmp_path / 'missing.json')
    assert state['step'] == 'provider-mode'
    assert state['complete'] is False


def test_panel_text_contains_commands():
    text = onboarding_state.onboarding_panel_text(onboarding_state.default_onboarding())
    assert '/setup provider <name>' in text and '/setup no-provider' in text
    assert 'Current step:' in text
