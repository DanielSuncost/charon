import pytest

from charon.conversation.conversation_engine import ConversationEngine
from charon.providers import ModelInfo, StreamDelta


class CapturingProvider:
    def __init__(self):
        self.thinking_level = None

    async def stream(self, messages, model, system_prompt, tools=None, thinking_level='off', max_tokens=16384):
        self.thinking_level = thinking_level
        yield StreamDelta(type='text', text='ok')
        yield StreamDelta(type='done')


@pytest.mark.asyncio
async def test_engine_passes_configured_thinking_level(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'onboarding.json').write_text('{"reasoning_effort":"high"}')

    provider = CapturingProvider()
    engine = ConversationEngine(
        provider,
        ModelInfo(provider='codex', model_id='gpt-5.6'),
        project_root=tmp_path,
        state_dir=state,
    )

    async for _ in engine.submit('hello'):
        pass

    assert provider.thinking_level == 'high'
