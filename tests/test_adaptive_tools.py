from __future__ import annotations

import asyncio
import json

from charon.conversation.conversation_engine import ConversationEngine
from charon.providers import ModelInfo, StreamDelta
from charon.tools import ALL_TOOL_DEFS, execute_tool


MODEL = ModelInfo(provider='mock', model_id='mock', context_window=100_000)


class CapturingProvider:
    def __init__(self):
        self.tool_names = []

    async def stream(self, **kwargs):
        self.tool_names.append([tool['name'] for tool in kwargs.get('tools') or []])
        yield StreamDelta(
            type='done',
            text=json.dumps({'usage': {}, 'stop_reason': 'end_turn'}),
        )


def test_default_payload_is_smaller_than_full_registry(tmp_path, monkeypatch):
    monkeypatch.delenv('CHARON_ADAPTIVE_TOOLS', raising=False)
    provider = CapturingProvider()
    engine = ConversationEngine(provider, MODEL, project_root=tmp_path)

    asyncio.run(engine.submit_and_collect('Read the configuration and fix the bug'))

    assert 'Read' in provider.tool_names[0]
    assert 'ToolCatalog' in provider.tool_names[0]
    assert len(provider.tool_names[0]) < len(ALL_TOOL_DEFS)
    assert 'Paper' not in provider.tool_names[0]


def test_research_intent_activates_research_tools_before_request(tmp_path):
    provider = CapturingProvider()
    engine = ConversationEngine(provider, MODEL, project_root=tmp_path)

    asyncio.run(engine.submit_and_collect('Research the latest papers and cite sources'))

    names = provider.tool_names[0]
    assert {'Search', 'Web', 'Paper', 'SourceDiscovery'} <= set(names)


def test_catalog_can_enable_specialized_tool_for_next_turn(tmp_path):
    provider = CapturingProvider()
    engine = ConversationEngine(provider, MODEL, project_root=tmp_path)

    result = execute_tool(
        'ToolCatalog',
        {'action': 'enable', 'names': ['Timeline']},
        engine.tool_context,
    )

    assert not result.is_error
    assert 'Timeline' in {tool['name'] for tool in engine.tools}


def test_adaptive_tools_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv('CHARON_ADAPTIVE_TOOLS', '0')
    provider = CapturingProvider()
    engine = ConversationEngine(provider, MODEL, project_root=tmp_path)

    asyncio.run(engine.submit_and_collect('hello'))

    assert len(provider.tool_names[0]) == len(engine._available_tools)
