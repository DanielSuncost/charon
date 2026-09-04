"""Focused tests for enforcing explicit model routes at execution time."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from charon.agents import agent_runtime
from charon.providers import ModelInfo, provider_bridge


def _agent(project) -> dict:
    return {
        'id': 'AG-route',
        'name': 'route-agent',
        'mode': 'persistent',
        'goal': 'execute the assigned task',
        'project': str(project),
        'status': 'running',
        'role': 'charon',
    }


def _task(project, route: dict, *, task_id: str = 'task-route') -> dict:
    return {
        'id': task_id,
        'instruction': 'Complete the routed task',
        'status': 'in_progress',
        'task_type': 'agent_task',
        'owner_agent_id': 'AG-route',
        'project': str(project),
        'model_route': dict(route),
    }


def test_provider_override_honors_exact_openai_compatible_endpoint(
    tmp_path,
):
    (tmp_path / 'onboarding.json').write_text(
        '{"provider": "api", "provider_base_url": '
        '"https://models.example.test/v1", "api_key": "test-key"}'
    )
    route = {
        'candidate_id': 'remote-fast',
        'provider': 'openai-compatible',
        'model_id': 'exact-model-v2',
        'context_window': 131_072,
        'base_url': 'https://models.example.test/v1/',
    }

    provider, model, ready = provider_bridge.create_provider_and_model(
        tmp_path,
        route_override=route,
    )

    assert ready is True
    assert model == ModelInfo(
        provider='openai-compatible',
        model_id='exact-model-v2',
        context_window=131_072,
        supports_thinking=False,
    )
    assert provider._base_url == 'https://models.example.test/v1'
    assert provider_bridge.describe_provider_endpoint(provider, model) == {
        'candidate_id': 'remote-fast',
        'provider': 'openai-compatible',
        'model_id': 'exact-model-v2',
        'context_window': 131_072,
        'base_url': 'https://models.example.test/v1',
    }


def test_unsupported_provider_override_fails_explicitly(tmp_path):
    with pytest.raises(provider_bridge.ProviderRouteError, match='unsupported'):
        provider_bridge.create_provider_and_model(
            tmp_path,
            route_override={
                'provider': 'unknown-service',
                'model_id': 'some-model',
            },
        )


def test_provider_override_rejects_embedded_credentials(tmp_path):
    with pytest.raises(provider_bridge.ProviderRouteError, match='must not contain'):
        provider_bridge.create_provider_and_model(
            tmp_path,
            route_override={
                'provider': 'openai',
                'model_id': 'gpt-test',
                'api_key': 'must-not-travel-with-route',
            },
        )


def test_provider_override_requires_integral_context_window(tmp_path):
    with pytest.raises(provider_bridge.ProviderRouteError, match='positive integer'):
        provider_bridge.resolve_route_override(
            tmp_path,
            {
                'provider': 'local',
                'model_id': 'local-model',
                'context_window': 1.5,
            },
        )


@pytest.mark.parametrize(
    'base_url',
    [
        'https://user:secret@models.example.test/v1',
        'https://models.example.test/v1?token=secret',
        'https://models.example.test/v1#private',
    ],
)
def test_provider_override_rejects_unsafe_base_url(tmp_path, base_url):
    with pytest.raises(provider_bridge.ProviderRouteError, match='credential-free'):
        provider_bridge.resolve_route_override(
            tmp_path,
            {
                'provider': 'local',
                'model_id': 'local-model',
                'base_url': base_url,
            },
        )


def test_openai_route_does_not_reuse_codex_onboarding_token(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    (tmp_path / 'onboarding.json').write_text(
        '{"provider": "codex", "api_key": "codex-oauth-token"}'
    )

    with pytest.raises(provider_bridge.ProviderRouteError, match='missing credentials'):
        provider_bridge.resolve_route_override(
            tmp_path,
            {
                'provider': 'openai',
                'model_id': 'gpt-test',
            },
        )


def test_openai_route_does_not_reuse_custom_endpoint_secret(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    (tmp_path / 'onboarding.json').write_text(
        '{"provider": "api", "provider_base_url": '
        '"https://models.example.test/v1", '
        '"api_key": "custom-endpoint-secret"}'
    )

    with pytest.raises(provider_bridge.ProviderRouteError, match='missing credentials'):
        provider_bridge.resolve_route_override(
            tmp_path,
            {
                'provider': 'openai',
                'model_id': 'gpt-test',
            },
        )


def test_custom_route_does_not_send_openai_key_to_unconfigured_endpoint(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv('OPENAI_API_KEY', 'must-stay-on-openai')

    with pytest.raises(
        provider_bridge.ProviderRouteError,
        match='configured custom endpoint',
    ):
        provider_bridge.resolve_route_override(
            tmp_path,
            {
                'provider': 'openai-compatible',
                'model_id': 'remote-model',
                'base_url': 'https://untrusted.example.test/v1',
            },
        )


def test_local_route_cannot_redirect_configured_local_key(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv('CHARON_LOCAL_API_KEY', 'local-secret')

    with pytest.raises(
        provider_bridge.ProviderRouteError,
        match='configured local endpoint',
    ):
        provider_bridge.resolve_route_override(
            tmp_path,
            {
                'provider': 'local',
                'model_id': 'remote-model',
                'base_url': 'https://untrusted.example.test/v1',
            },
        )


def test_routed_engine_cache_identity_changes_when_credentials_rotate(
    tmp_path,
    monkeypatch,
):
    route = {
        'provider': 'local',
        'model_id': 'local-model',
        'base_url': 'http://127.0.0.1:1234/v1',
    }
    monkeypatch.setenv('CHARON_LOCAL_API_KEY', 'local-key-one')
    first = provider_bridge.resolve_route_override(tmp_path, route)
    monkeypatch.setenv('CHARON_LOCAL_API_KEY', 'local-key-two')
    second = provider_bridge.resolve_route_override(tmp_path, route)

    assert first['credential_fingerprint'] != second['credential_fingerprint']
    first_key = agent_runtime._engine_cache_key(
        tmp_path,
        'AG-route',
        tmp_path,
        first['selected_endpoint'],
        first['credential_fingerprint'],
    )
    second_key = agent_runtime._engine_cache_key(
        tmp_path,
        'AG-route',
        tmp_path,
        second['selected_endpoint'],
        second['credential_fingerprint'],
    )
    assert first_key != second_key


def test_openai_route_cannot_override_fixed_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'openai-key')

    with pytest.raises(provider_bridge.ProviderRouteError, match='cannot honor'):
        provider_bridge.resolve_route_override(
            tmp_path,
            {
                'provider': 'openai',
                'model_id': 'gpt-test',
                'base_url': 'https://untrusted.example.test/v1',
            },
        )


def test_custom_route_requires_tls_away_from_loopback(tmp_path):
    (tmp_path / 'onboarding.json').write_text(
        '{"provider": "api", "provider_base_url": '
        '"http://models.example.test/v1", "api_key": "test-key"}'
    )

    with pytest.raises(provider_bridge.ProviderRouteError, match='requires TLS'):
        provider_bridge.resolve_route_override(
            tmp_path,
            {
                'provider': 'api',
                'model_id': 'remote-model',
                'base_url': 'http://models.example.test/v1',
            },
        )


def test_route_override_forwarding_and_cache_isolation(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    project_a = tmp_path / 'project-a'
    project_b = tmp_path / 'project-b'
    state_dir.mkdir()
    project_a.mkdir()
    project_b.mkdir()
    forwarded: list[dict] = []

    def fake_create_provider_and_model(
        requested_state_dir,
        session_id=None,
        route_override=None,
    ):
        assert requested_state_dir == state_dir
        assert session_id is None
        forwarded.append(dict(route_override))
        config = provider_bridge.resolve_route_override(
            state_dir,
            route_override,
        )
        endpoint = dict(config['selected_endpoint'])
        provider = SimpleNamespace(_charon_endpoint=endpoint)
        model = ModelInfo(
            provider=endpoint['provider'],
            model_id=endpoint['model_id'],
            context_window=endpoint['context_window'],
        )
        return provider, model, True

    class FakeEngine:
        def __init__(self, **kwargs):
            self.provider = kwargs['provider']
            self.model = kwargs['model']
            self.project_root = kwargs['project_root'].resolve()
            self.system_prompt = kwargs['system_prompt']

        def update_system_prompt(self, value):
            self.system_prompt = value

    monkeypatch.setattr(
        provider_bridge,
        'create_provider_and_model',
        fake_create_provider_and_model,
    )
    monkeypatch.setattr(
        'charon.conversation.conversation_engine.ConversationEngine',
        FakeEngine,
    )
    monkeypatch.setattr(
        agent_runtime,
        '_build_task_system_prompt',
        lambda state, agent, task: f"prompt:{task['id']}",
    )
    agent_runtime._agent_engines.clear()

    route_a = {
        'provider': 'local',
        'model_id': 'model-a',
        'context_window': 4096,
        'base_url': 'http://127.0.0.1:1234/v1',
    }
    route_b = {**route_a, 'model_id': 'model-b'}
    agent = _agent(project_a)

    engine_a, ready_a = agent_runtime._get_or_create_engine(
        state_dir,
        agent,
        _task(project_a, route_a, task_id='task-a1'),
    )
    engine_a_again, ready_a_again = agent_runtime._get_or_create_engine(
        state_dir,
        agent,
        _task(project_a, route_a, task_id='task-a2'),
    )
    engine_b, ready_b = agent_runtime._get_or_create_engine(
        state_dir,
        agent,
        _task(project_a, route_b, task_id='task-b'),
    )
    engine_other_project, ready_other_project = (
        agent_runtime._get_or_create_engine(
            state_dir,
            agent,
            _task(project_b, route_a, task_id='task-project-b'),
        )
    )

    assert all((ready_a, ready_a_again, ready_b, ready_other_project))
    assert engine_a_again is engine_a
    assert engine_b is not engine_a
    assert engine_other_project is not engine_a
    assert forwarded == [route_a, route_b, route_a]


def test_unavailable_route_fails_without_heuristic_fallback(
    tmp_path,
    monkeypatch,
):
    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setattr(
        agent_runtime,
        '_build_task_system_prompt',
        lambda state, agent, task: 'routed task',
    )
    agent_runtime._agent_engines.clear()
    target = tmp_path / 'must-not-be-written.txt'
    task = _task(
        tmp_path,
        {
            'provider': 'openai',
            'model_id': 'gpt-test',
            'context_window': 8192,
        },
    )
    task['instruction'] = f'write: {target.name} | heuristic fallback ran'

    ok, result = agent_runtime.run_task_tick(
        state_dir,
        task,
        agent=_agent(tmp_path),
    )

    assert ok is False
    assert result['status'] == 'task_failed'
    assert 'missing credentials' in result['error']
    assert not target.exists()
    assert task['attempts'][-1]['status'] == 'failed'
    assert task['selected_model']['provider'] == 'openai'
    assert task['executed_model'] is None
    assert task['route_honored'] is False


def test_run_records_selected_and_executed_endpoint(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    selected = {
        'candidate_id': 'local-large',
        'provider': 'ollama',
        'model_id': 'model-large',
        'context_window': 32_768,
        'base_url': 'http://127.0.0.1:11434/v1',
    }
    executed = dict(selected)
    engine = SimpleNamespace(
        _charon_selected_endpoint=selected,
        _charon_executed_endpoint=executed,
    )
    task = _task(tmp_path, selected)

    monkeypatch.setattr(
        agent_runtime,
        '_get_or_create_engine',
        lambda state, agent, task: (engine, True),
    )
    monkeypatch.setattr(
        agent_runtime,
        '_run_task_with_engine',
        lambda state, task, agent, engine: (
            True,
            {
                'status': 'task_succeeded',
                'summary': 'completed on selected endpoint',
                'attempt_id': 'att-routed',
            },
        ),
    )

    ok, result = agent_runtime.run_task_tick(
        state_dir,
        task,
        agent=_agent(tmp_path),
    )

    assert ok is True
    assert result['selected_endpoint'] == selected
    assert result['executed_endpoint'] == executed
    assert result['executed_model'] == executed
    assert result['route_honored'] is True
    assert task['selected_model'] == selected
    assert task['executed_model'] == executed
    assert task['route_honored'] is True
