import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))
sys.path.insert(0, str(ROOT))

import worker_provider


def _write_auth(state_dir: Path, *, codex: bool = False):
    auth_dir = state_dir / 'auth'
    auth_dir.mkdir(parents=True, exist_ok=True)
    providers = {}
    if codex:
        providers['openai-codex'] = {
            'tokens': {'access_token': 'test-token'},
            'auth_type': 'oauth',
        }
    (auth_dir / 'auth.json').write_text(json.dumps({
        'version': 1,
        'active_provider': 'openai-codex' if codex else '',
        'providers': providers,
    }))


def test_apply_codex_worker_provider(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    _write_auth(state, codex=True)

    result = worker_provider.apply_worker_provider_choice(state, 'codex')
    assert result['provider'] == 'codex'

    reg = json.loads((state / 'model_registry.json').read_text())
    assert reg['shade_model_mode'] == 'fixed'
    assert reg['shade_provider'] == 'codex'
    assert reg['shade_model'] == 'gpt-5.4'


def test_apply_lmstudio_worker_provider(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)

    result = worker_provider.apply_worker_provider_choice(state, 'lmstudio')
    assert result['provider'] == 'lmstudio'

    reg = json.loads((state / 'model_registry.json').read_text())
    assert reg['shade_model_mode'] == 'fixed'
    assert reg['shade_provider'] == 'lmstudio'
    assert reg['shade_model'] == 'qwen3-30b-a3b'
    assert reg['shade_base_url'] == 'http://127.0.0.1:1234/v1'
    assert reg['shade_api_key'] == 'not-needed'
