import json
from pathlib import Path

from charon.providers import worker_provider


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


def _write_bad_fixed_registry(state_dir: Path):
    (state_dir / 'model_registry.json').write_text(json.dumps({
        'shade_model_mode': 'fixed',
        'shade_provider': 'openai',
        'shade_model': 'lmstudio/qwen-test',
        'shade_api_key': None,
        'shade_base_url': None,
    }))


def test_background_flow_request_creates_worker_provider_clarification(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    _write_auth(state, codex=True)
    _write_bad_fixed_registry(state)

    result = worker_provider.request_worker_provider_for_background_flow(
        state,
        purpose='Libris worker tasks',
        agent_id='AG-LIB',
        project_root=tmp_path,
    )
    assert result['ok'] is False
    assert result['reason'] == 'mismatch'
    assert 'codex' in result['available_providers']
    assert 'lmstudio' in result['available_providers']
    assert result['clarification']['clarification_id']

    clar_data = json.loads((state / 'clarifications.json').read_text())
    assert clar_data['items']
    assert 'Libris worker tasks' in clar_data['items'][0]['question']
