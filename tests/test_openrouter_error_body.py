"""Regression coverage for the SWE-bench OpenRouter provider rejection."""
import json

import httpx
import pytest

from charon.providers import Message, ModelInfo
from charon.providers.httpx_openai import HttpxOpenAIProvider
from charon.tools import EDIT_TOOL_DEF, ToolContext, execute_edit


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [
    json.dumps({'error': {'message': 'Provider returned error', 'metadata': {
        'provider_name': 'Amazon Bedrock', 'raw': 'upstream detail ' * 100,
    }}}),
    '<html><title>Bad Gateway</title>' + 'upstream detail ' * 100 + '</html>',
])
async def test_full_error_survives_in_metrics_diagnostics_and_attempt_log(
    tmp_path, monkeypatch, capsys, raw,
):
    monkeypatch.setenv('CHARON_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('CHARON_EMIT_REQUEST_CONFIG', '1')
    provider = HttpxOpenAIProvider(
        base_url='https://openrouter.ai/api/v1', api_key='test-secret',
        provider_name='openrouter',
    )
    # A reflected credential must never reach any log destination.
    response_body = raw + '\ntest-secret'
    provider._mock_handler = lambda request: httpx.Response(400, text=response_body)
    try:
        deltas = [delta async for delta in provider.stream(
            messages=[Message(role='user', content='test')],
            model=ModelInfo(provider='openrouter', model_id='anthropic/claude-haiku-5.5'),
            system_prompt='test', thinking_level='medium',
        )]
    finally:
        await provider.aclose()
    expected = raw + '\n[REDACTED]'
    event = provider.last_request_metrics['provider_error']
    assert event['raw_body'] == expected
    assert event['status_code'] == 400
    stderr = capsys.readouterr().err
    emitted = [json.loads(line) for line in stderr.splitlines()]
    assert next(item['provider_error'] for item in emitted if 'provider_error' in item) == event
    persisted = json.loads((tmp_path / 'diagnostics.jsonl').read_text().splitlines()[-1])
    assert persisted['raw_body'] == expected
    assert 'test-secret' not in stderr
    assert all('test-secret' not in delta.error for delta in deltas if delta.type == 'error')
    assert deltas[0].status_code == 400
    assert deltas[0].retryable is False


def test_edit_schema_accepted_without_weakening_runtime_validation(tmp_path):
    assert not ({'anyOf', 'oneOf', 'allOf'} & EDIT_TOOL_DEF['input_schema'].keys())
    target = tmp_path / 'example.txt'
    target.write_text('original\n')
    result = execute_edit({'path': str(target)}, ToolContext(project_root=tmp_path))
    assert result.is_error
    assert target.read_text() == 'original\n'


@pytest.mark.parametrize('params', [{}, {'oldText': 'original'}, {'newText': 'changed'}])
def test_edit_alternatives_actionable_before_file_access(tmp_path, params):
    result = execute_edit({'path': 'absent.txt', **params}, ToolContext(project_root=tmp_path))
    assert result.is_error
    assert 'oldText and newText' in result.content
    assert 'edits array' in result.content


def test_registered_tool_schemas_have_no_top_level_combinators():
    from charon.tools import ALL_TOOL_DEFS
    for tool in ALL_TOOL_DEFS:
        schema = tool.get('input_schema', tool.get('parameters', {}))
        assert schema.get('type') == 'object', tool['name']
        assert not ({'anyOf', 'oneOf', 'allOf'} & schema.keys()), tool['name']
    assert 'anyOf' in EDIT_TOOL_DEF['input_schema']['properties']['edits']['items']


@pytest.mark.asyncio
async def test_upstream_provider_recorded_per_request(monkeypatch, capsys):
    monkeypatch.setenv('CHARON_EMIT_REQUEST_CONFIG', '1')
    provider = HttpxOpenAIProvider(
        base_url='https://openrouter.ai/api/v1', api_key='test-secret',
        provider_name='openrouter',
    )
    chunks = [
        {'id': 'gen-1', 'provider': 'Amazon Bedrock', 'model': 'anthropic/claude-haiku-5.5',
         'choices': [{'delta': {'content': 'ok'}}]},
        {'id': 'gen-1', 'provider': 'Amazon Bedrock', 'choices': [],
         'usage': {'prompt_tokens': 3, 'completion_tokens': 1}},
    ]
    body = ''.join(f'data: {json.dumps(c)}\n\n' for c in chunks) + 'data: [DONE]\n\n'
    provider._mock_handler = lambda request: httpx.Response(
        200, text=body, headers={'content-type': 'text/event-stream'})
    try:
        [delta async for delta in provider.stream(
            messages=[Message(role='user', content='test')],
            model=ModelInfo(provider='openrouter', model_id='anthropic/claude-haiku-5.5'),
            system_prompt='test', thinking_level='medium',
        )]
    finally:
        await provider.aclose()
    expected = {
        'provider': 'openrouter', 'model': 'anthropic/claude-haiku-5.5',
        'upstream': 'Amazon Bedrock', 'generation_id': 'gen-1',
        'response_model': 'anthropic/claude-haiku-5.5',
    }
    assert provider.last_request_metrics['provider_response'] == expected
    emitted = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert {'provider_response': expected} in emitted
