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


class RunProcessThenStopProvider:
    """Calls RunProcess on the first request, then records the next payload."""

    def __init__(self):
        self.tool_names = []

    async def stream(self, **kwargs):
        from charon.providers import ToolCall

        self.tool_names.append([tool['name'] for tool in kwargs.get('tools') or []])
        if len(self.tool_names) == 1:
            yield StreamDelta(
                type='tool_call',
                tool_call=ToolCall(id='call-1', name='RunProcess', arguments={'command': 'true'}),
            )
            stop = 'tool_use'
        else:
            stop = 'end_turn'
        yield StreamDelta(type='done', text=json.dumps({'usage': {}, 'stop_reason': stop}))


def test_process_followups_are_deferred_until_a_process_runs(tmp_path, monkeypatch):
    from charon import tools
    from charon.tools import ToolResult

    monkeypatch.delenv('CHARON_ADAPTIVE_TOOLS', raising=False)
    monkeypatch.setenv('CHARON_SKIP_APPROVAL', '1')
    monkeypatch.setitem(
        tools.TOOL_EXECUTORS, 'RunProcess', lambda params, ctx: ToolResult(content='started proc-1'),
    )
    provider = RunProcessThenStopProvider()
    engine = ConversationEngine(provider, MODEL, project_root=tmp_path, interactive=False)

    asyncio.run(engine.submit_and_collect('Fix the parser bug'))

    followups = {'ProcessStatus', 'ProcessLogs', 'StopProcess'}
    assert 'RunProcess' in provider.tool_names[0]
    assert not followups & set(provider.tool_names[0])
    assert followups <= set(provider.tool_names[1])


def test_process_intent_enables_followups_before_request(tmp_path):
    provider = CapturingProvider()
    engine = ConversationEngine(provider, MODEL, project_root=tmp_path)

    asyncio.run(engine.submit_and_collect('Is the dev server still running?'))

    assert {'ProcessStatus', 'ProcessLogs', 'StopProcess'} <= set(provider.tool_names[0])


def test_tool_names_activate_only_as_whole_words(tmp_path):
    provider = CapturingProvider()
    engine = ConversationEngine(provider, MODEL, project_root=tmp_path)

    asyncio.run(engine.submit_and_collect('fix the txt parser and explain the next step'))
    assert 'X' not in provider.tool_names[0]

    asyncio.run(engine.submit_and_collect('use the X tool to check my bookmarks'))
    assert 'X' in provider.tool_names[-1]
