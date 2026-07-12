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


def test_spawn_batch_worker_crash_marks_batch_failed(tmp_path, monkeypatch):
    """Regression: a crash in the background worker used to leave the batch
    'running' forever; it must reach the terminal 'failed' state."""
    import re
    import time

    from charon.automation import batch_orchestrator
    from charon.providers import worker_provider

    ctx = _ctx(tmp_path)
    monkeypatch.setattr(
        worker_provider, 'ensure_worker_provider_or_request_clarification',
        lambda *a, **k: {'ok': True},
    )

    def _boom(state_dir, batch_id, **kwargs):
        raise RuntimeError('worker exploded')

    monkeypatch.setattr(batch_orchestrator, 'run_batch_worker', _boom)

    result = execute_spawn_batch({
        'goal': 'crash test',
        'tasks': [{'title': 'One', 'instruction': 'Do one thing'}],
    }, ctx)
    assert not result.is_error
    bid = re.search(r'batch-[0-9a-f]+', result.content).group(0)

    batch = None
    deadline = time.time() + 5
    while time.time() < deadline:
        batch = batch_orchestrator.get_batch(ctx.state_dir, bid)
        if batch and batch.get('status') == 'failed':
            break
        time.sleep(0.05)

    assert batch is not None
    assert batch['status'] == 'failed'
    assert 'worker exploded' in batch.get('error', '')
    assert all(t.get('status') == 'failed' for t in batch['tasks'])
