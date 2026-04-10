import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))
sys.path.insert(0, str(ROOT))

from tools import ToolContext
from tools.shade_tool import execute_spawn_shade
from tools.x_tool import execute_x
import worker_provider


def _ctx(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    return ToolContext(project_root=tmp_path, state_dir=state, agent_id='AG-1')


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


def test_list_available_worker_providers_offers_codex_and_lmstudio(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    _write_auth(state, codex=True)
    available = worker_provider.list_available_worker_providers(state)
    assert 'codex' in available
    assert 'lmstudio' in available


def test_spawn_shade_requests_provider_choice_when_fixed_provider_mismatch(tmp_path):
    ctx = _ctx(tmp_path)
    _write_auth(ctx.state_dir, codex=True)
    _write_bad_fixed_registry(ctx.state_dir)

    result = execute_spawn_shade({'goal': 'Test worker task'}, ctx)
    assert result.is_error
    assert 'No usable provider is configured for shades.' in result.content
    assert '- codex' in result.content
    assert '- lmstudio' in result.content

    clar_path = ctx.state_dir / 'clarifications.json'
    data = json.loads(clar_path.read_text())
    assert data['items']
    assert 'Which provider should I use for worker tasks?' in data['items'][0]['question']


def test_x_triage_requests_provider_choice_when_fixed_provider_mismatch(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _write_auth(ctx.state_dir, codex=True)
    _write_bad_fixed_registry(ctx.state_dir)

    result = execute_x({'action': 'triage_new_bookmarks', 'project': 'charon', 'limit': 5}, ctx)
    assert result.is_error
    payload = json.loads(result.content)
    assert payload['status'] == 'needs_provider_choice'
    assert payload['reason'] == 'mismatch'
    assert 'codex' in payload['available_providers']
    assert 'lmstudio' in payload['available_providers']
    assert payload['clarification']['clarification_id']
