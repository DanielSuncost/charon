"""Tests for the unified agent runtime — conversation engine integration.

Verifies that:
1. When onboarding is complete with a provider, run_task_tick uses ConversationEngine
2. When onboarding is incomplete, run_task_tick falls back to heuristic
3. Provider bridge correctly reads onboarding + auth config
4. Per-agent engine caching works
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))


def _load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


agent_runtime = _load_mod('agent_runtime_unified_test', ROOT / 'apps' / 'core-daemon' / 'agent_runtime.py')
provider_bridge = _load_mod('provider_bridge_test', ROOT / 'apps' / 'core-daemon' / 'provider_bridge.py')


def _write_onboarding(state_dir: Path, **overrides):
    defaults = {
        'complete': True,
        'step': 'done',
        'provider_mode': 'provider',
        'provider': 'claude-code',
        'model': 'claude-sonnet-4-20250514',
        'provider_model': 'claude-sonnet-4-20250514',
    }
    defaults.update(overrides)
    ob_file = state_dir / 'onboarding.json'
    ob_file.parent.mkdir(parents=True, exist_ok=True)
    ob_file.write_text(json.dumps(defaults))


def _write_auth(state_dir: Path, provider_id: str = 'anthropic', access_token: str = 'test-token'):
    auth_file = state_dir / 'auth' / 'auth.json'
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text(json.dumps({
        'version': 1,
        'active_provider': provider_id,
        'providers': {
            provider_id: {
                'tokens': {'access_token': access_token},
                'auth_type': 'oauth',
            },
        },
    }))


def _sample_agent(agent_id='AG-0001'):
    return {
        'id': agent_id,
        'name': 'test-agent',
        'mode': 'persistent',
        'goal': 'test',
        'project': '',
        'status': 'running',
        'role': 'charon',
    }


def _sample_task(task_id='task-001', instruction='Fix the bug'):
    return {
        'id': task_id,
        'title': 'Test task',
        'instruction': instruction,
        'status': 'in_progress',
        'task_type': 'agent_task',
        'owner_agent_id': 'AG-0001',
    }


# ============================================================================
# Provider bridge tests
# ============================================================================

class TestProviderBridge:
    def test_unconfigured_returns_not_ready(self, tmp_path):
        config = provider_bridge.resolve_provider_config(tmp_path)
        assert config['ready'] is False
        assert config['provider_name'] == 'local'

    def test_no_provider_mode(self, tmp_path):
        _write_onboarding(tmp_path, provider_mode='no-provider', complete=True)
        config = provider_bridge.resolve_provider_config(tmp_path)
        assert config['ready'] is False

    def test_incomplete_setup(self, tmp_path):
        _write_onboarding(tmp_path, complete=False)
        config = provider_bridge.resolve_provider_config(tmp_path)
        assert config['ready'] is False

    def test_anthropic_with_oauth(self, tmp_path):
        _write_onboarding(tmp_path, provider='claude-code')
        _write_auth(tmp_path, 'anthropic', 'sk-ant-test')
        config = provider_bridge.resolve_provider_config(tmp_path)
        assert config['ready'] is True
        assert config['provider_name'] == 'anthropic'
        assert config['api_key'] == 'sk-ant-test'
        assert config['model_id'] == 'claude-sonnet-4-20250514'
        assert config['supports_thinking'] is True

    def test_codex_with_oauth(self, tmp_path):
        _write_onboarding(tmp_path, provider='codex', model='gpt-4o')
        _write_auth(tmp_path, 'openai-codex', 'sk-test')
        config = provider_bridge.resolve_provider_config(tmp_path)
        assert config['ready'] is True
        assert config['provider_name'] == 'openai'
        assert config['api_key'] == 'sk-test'

    def test_local_provider(self, tmp_path):
        _write_onboarding(tmp_path, provider='lmstudio', model='qwen3-30b-a3b')
        config = provider_bridge.resolve_provider_config(tmp_path)
        assert config['ready'] is True
        assert config['provider_name'] == 'local'
        assert config['api_key'] == 'not-needed'

    def test_api_key_from_onboarding(self, tmp_path):
        _write_onboarding(tmp_path, provider='api', api_key='my-key', model='gpt-4o')
        config = provider_bridge.resolve_provider_config(tmp_path)
        assert config['ready'] is True
        assert config['api_key'] == 'my-key'

    def test_env_var_overrides(self, tmp_path, monkeypatch):
        _write_onboarding(tmp_path, provider='claude-code')
        _write_auth(tmp_path, 'anthropic', 'oauth-token')
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'env-key')
        config = provider_bridge.resolve_provider_config(tmp_path)
        assert config['api_key'] == 'env-key'  # env var wins

    def test_custom_base_url(self, tmp_path):
        _write_onboarding(tmp_path, provider='api', provider_base_url='http://my-server:8080/v1', api_key='k')
        config = provider_bridge.resolve_provider_config(tmp_path)
        assert config['base_url'] == 'http://my-server:8080/v1'

    def test_create_provider_and_model(self, tmp_path):
        _write_onboarding(tmp_path, provider='lmstudio', model='qwen3-30b-a3b', provider_model='qwen3-30b-a3b')
        provider, model, ready = provider_bridge.create_provider_and_model(tmp_path)
        assert ready is True
        assert model.model_id == 'qwen3-30b-a3b'
        assert model.provider == 'local'

    def test_session_override_provider_config_isolated_from_global(self, tmp_path):
        _write_onboarding(tmp_path, provider='codex', model='gpt-5.4', provider_model='gpt-5.4')
        provider_bridge.save_session_provider_config(tmp_path, 'sess-a', {
            'complete': True,
            'step': 'done',
            'provider_mode': 'provider',
            'provider': 'lmstudio',
            'model': 'qwen3-30b-a3b',
            'provider_model': 'qwen3-30b-a3b',
        })

        global_cfg = provider_bridge.resolve_provider_config(tmp_path)
        session_cfg = provider_bridge.resolve_provider_config(tmp_path, session_id='sess-a')

        assert global_cfg['provider_raw'] == 'codex'
        assert global_cfg['model_id'] == 'gpt-5.4'
        assert session_cfg['provider_raw'] == 'lmstudio'
        assert session_cfg['provider_name'] == 'local'
        assert session_cfg['model_id'] == 'qwen3-30b-a3b'
        assert session_cfg['session_override'] is True

    def test_session_override_can_be_cleared(self, tmp_path):
        _write_onboarding(tmp_path, provider='codex', model='gpt-5.4', provider_model='gpt-5.4')
        provider_bridge.save_session_provider_config(tmp_path, 'sess-a', {
            'complete': True,
            'step': 'done',
            'provider_mode': 'provider',
            'provider': 'lmstudio',
            'model': 'qwen3-30b-a3b',
            'provider_model': 'qwen3-30b-a3b',
        })
        assert provider_bridge.load_session_provider_config(tmp_path, 'sess-a')['provider'] == 'lmstudio'

        provider_bridge.clear_session_provider_config(tmp_path, 'sess-a')

        assert provider_bridge.load_session_provider_config(tmp_path, 'sess-a') == {}
        cfg = provider_bridge.resolve_provider_config(tmp_path, session_id='sess-a')
        assert cfg['provider_raw'] == 'codex'
        assert cfg['model_id'] == 'gpt-5.4'


# ============================================================================
# Planner mode detection
# ============================================================================

class TestPlannerMode:
    def test_heuristic_when_unconfigured(self, tmp_path):
        assert agent_runtime._resolve_planner_mode(tmp_path) == 'heuristic'

    def test_heuristic_when_no_provider(self, tmp_path):
        _write_onboarding(tmp_path, provider_mode='no-provider', complete=True)
        assert agent_runtime._resolve_planner_mode(tmp_path) == 'heuristic'

    def test_llm_when_configured(self, tmp_path):
        _write_onboarding(tmp_path, provider='claude-code', provider_mode='provider', complete=True)
        assert agent_runtime._resolve_planner_mode(tmp_path) == 'llm'

    def test_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv('CHARON_AGENT_PLANNER', 'heuristic')
        _write_onboarding(tmp_path, provider='claude-code', provider_mode='provider', complete=True)
        assert agent_runtime._resolve_planner_mode(tmp_path) == 'heuristic'


# ============================================================================
# Heuristic fallback (existing behavior preserved)
# ============================================================================

class TestHeuristicFallback:
    def test_heuristic_mode_works(self, tmp_path):
        state_dir = tmp_path / 'state'
        state_dir.mkdir()
        # No onboarding → heuristic mode
        agent = _sample_agent()
        task = _sample_task(instruction='Fix the bug')

        ok, result = agent_runtime.run_task_tick(state_dir, task, agent=agent)
        assert ok is True
        assert 'summary' in result
        assert result.get('attempt_id')

    def test_run_prefix_still_works(self, tmp_path):
        state_dir = tmp_path / 'state'
        state_dir.mkdir()
        agent = _sample_agent()
        agent['project'] = str(tmp_path)
        task = _sample_task(instruction='run: echo hello')

        ok, result = agent_runtime.run_task_tick(state_dir, task, agent=agent)
        assert ok is True
        assert 'hello' in result.get('summary', '')


# ============================================================================
# Engine path (mocked provider)
# ============================================================================

class TestEnginePath:
    def test_dispatches_to_engine_when_llm_mode(self, tmp_path):
        """When onboarding is complete, run_task_tick should use ConversationEngine."""
        state_dir = tmp_path / 'state'
        state_dir.mkdir()
        _write_onboarding(state_dir, provider='lmstudio', model='test-model')

        agent = _sample_agent()
        agent['project'] = str(tmp_path)
        task = _sample_task(instruction='Describe what you see')

        # Mock the engine creation to use our mock provider
        mock_events = [
            MagicMock(type='turn_start', data={'turn': 1}),
            MagicMock(type='text_delta', data={'text': 'I see a project directory.'}),
            MagicMock(type='message_end', data={'role': 'assistant'}),
            MagicMock(type='done', data={'total_turns': 1, 'message_count': 2}),
        ]

        async def mock_submit(prompt):
            for evt in mock_events:
                yield evt

        mock_engine = MagicMock()
        mock_engine.submit = mock_submit
        mock_engine.project_root = tmp_path.resolve()

        # Clear engine cache
        agent_runtime._agent_engines.clear()

        with patch.object(agent_runtime, '_get_or_create_engine', return_value=(mock_engine, True)):
            ok, result = agent_runtime.run_task_tick(state_dir, task, agent=agent)

        assert ok is True
        assert 'I see a project directory.' in result.get('summary', '')
        assert result.get('attempt_id')

    def test_falls_back_on_connection_error(self, tmp_path):
        """When provider can't connect, task should fail with clear error."""
        state_dir = tmp_path / 'state'
        state_dir.mkdir()
        _write_onboarding(state_dir, provider='lmstudio', model='test-model')

        agent = _sample_agent()
        agent['project'] = str(tmp_path)
        task = _sample_task(instruction='Do something')

        mock_events = [
            MagicMock(type='error', data={'error': 'Connection failed to http://127.0.0.1:1234/v1'}),
            MagicMock(type='done', data={'total_turns': 1, 'message_count': 2}),
        ]

        async def mock_submit(prompt):
            for evt in mock_events:
                yield evt

        mock_engine = MagicMock()
        mock_engine.submit = mock_submit
        mock_engine.project_root = tmp_path.resolve()

        agent_runtime._agent_engines.clear()

        with patch.object(agent_runtime, '_get_or_create_engine', return_value=(mock_engine, True)):
            ok, result = agent_runtime.run_task_tick(state_dir, task, agent=agent)

        assert ok is False
        assert 'Connection failed' in result.get('error', '')

    def test_engine_not_ready_falls_back(self, tmp_path):
        """When engine reports not ready, fall back to heuristic."""
        state_dir = tmp_path / 'state'
        state_dir.mkdir()
        _write_onboarding(state_dir, provider='claude-code')
        # No auth → engine returns not ready

        agent = _sample_agent()
        task = _sample_task(instruction='Do something')

        agent_runtime._agent_engines.clear()

        with patch.object(agent_runtime, '_get_or_create_engine', return_value=(None, False)):
            ok, result = agent_runtime.run_task_tick(state_dir, task, agent=agent)

        # Should fall through to heuristic
        assert ok is True
        assert 'summary' in result
