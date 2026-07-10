import json
from pathlib import Path

from charon.tools import ToolContext
from charon.tools.batch_tool import execute_spawn_batch


def _ctx(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    return ToolContext(project_root=tmp_path, state_dir=state, agent_id='AG-B')


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


def test_spawn_batch_requests_provider_choice_when_worker_provider_invalid(tmp_path):
    ctx = _ctx(tmp_path)
    _write_auth(ctx.state_dir, codex=True)
    _write_bad_fixed_registry(ctx.state_dir)

    result = execute_spawn_batch({
        'goal': 'triage bookmarks',
        'tasks': [{'title': 'One', 'instruction': 'Do one thing'}],
    }, ctx)
    assert result.is_error
    assert 'No usable provider is configured for batches.' in result.content
    assert '- codex' in result.content
    assert '- lmstudio' in result.content
