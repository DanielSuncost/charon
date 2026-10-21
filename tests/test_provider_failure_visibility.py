"""Provider rejections must explain the upstream cause in the UI event."""
import asyncio
import json

import httpx

from charon.providers import Message, ModelInfo
from charon.providers.httpx_openai import HttpxOpenAIProvider
from charon.tools import ALL_TOOL_DEFS, ToolContext, execute_edit


def test_registered_schemas_and_edit_validation(tmp_path):
    for tool in ALL_TOOL_DEFS:
        schema = tool['input_schema']
        assert not {'anyOf', 'oneOf', 'allOf'} & schema.keys(), tool['name']
    target = tmp_path / 'file.txt'
    target.write_text('original')
    for params in ({}, {'oldText': 'original'}, {'newText': 'changed'}, {'edits': []}):
        assert execute_edit({'path': str(target), **params}, ToolContext(project_root=tmp_path)).is_error
        assert target.read_text() == 'original'


def test_openrouter_upstream_error_reaches_ui_without_credentials():
    detail = 'input_schema does not support oneOf, allOf, or anyOf at the top level'
    raw = json.dumps({'error': {'message': 'Provider returned error', 'metadata': {
        'provider_name': 'Anthropic', 'raw': detail + ' secret-key',
    }}})

    async def run():
        provider = HttpxOpenAIProvider(base_url='https://openrouter.ai/api/v1', api_key='secret-key')
        provider._mock_handler = lambda request: httpx.Response(400, text=raw)
        try:
            events = [d async for d in provider.stream(
                messages=[Message(role='user', content='hello')],
                model=ModelInfo(provider='openrouter', model_id='anthropic/claude'),
                system_prompt='test',
            )]
            failure = next(d for d in events if d.type == 'error')
            assert detail in failure.error
            assert 'secret-key' not in failure.error
            assert failure.status_code == 400
            assert failure.retryable is False
            assert '[REDACTED]' in provider.last_request_metrics['provider_error']['raw_body']
        finally:
            await provider.aclose()
    asyncio.run(run())


def test_interrupt_keeps_old_tool_token_cancelled_for_next_submission(tmp_path):
    from charon.conversation.conversation_engine import ConversationEngine
    from charon.providers import StreamDelta

    class Provider:
        async def stream(self, **kwargs):
            yield StreamDelta(type='text', text='finished')
            yield StreamDelta(type='done', text='{}')

    async def run():
        engine = ConversationEngine(Provider(), ModelInfo(provider='mock', model_id='mock'),
                                    project_root=tmp_path, auto_compact=False)
        old_token = None
        async for event in engine.submit('first'):
            if event.type == 'turn_start':
                old_token = engine._turn_cancel_event
                engine.abort()
        assert old_token.is_set()
        async for _event in engine.submit('next'):
            pass
        assert engine._turn_cancel_event is not old_token
        assert old_token.is_set(), 'an old tool thread must remain cancelled'
        assert not engine._turn_cancel_event.is_set()
    asyncio.run(run())
