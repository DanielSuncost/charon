"""Focused checks for task guidance, unattended execution and loop budgets."""
import asyncio

import pytest

from charon.conversation.conversation_engine import ConversationEngine
from charon.conversation.task_policy import LoopGuard
from charon.providers import ModelInfo, StreamDelta, ToolCall
from charon.tools import ToolContext, ToolResult
from charon.tools.clarify_tool import execute_clarify


@pytest.fixture(autouse=True)
def no_execution_memory(monkeypatch):
    from charon.conversation import conversation_engine as module
    monkeypatch.setattr(module, 'record_tool_event', lambda **kwargs: None)


class Provider:
    def __init__(self, tool=None):
        self.tool = tool
        self.calls = 0
        self.prompts = []
        self.tools = []

    async def stream(self, **kwargs):
        self.calls += 1
        self.prompts.append(kwargs['system_prompt'])
        self.tools = kwargs['tools']
        if self.tool:
            yield StreamDelta(type='tool_call', tool_call=ToolCall(
                id=f'call-{self.calls}', name=self.tool, arguments={}))
        else:
            yield StreamDelta(type='text', text='Assumption: use the local fixture.')
        yield StreamDelta(type='done')


def engine(tmp_path, provider, **kwargs):
    return ConversationEngine(provider, ModelInfo(provider='mock', model_id='mock',
                              context_window=100000), project_root=tmp_path,
                              auto_compact=False, **kwargs)


def collect(engine):
    async def run():
        return [event async for event in engine.submit('Complete the task')]
    return asyncio.run(run())


def test_task_oracle_runtime_and_verification_guidance_reaches_custom_prompt(tmp_path):
    provider = Provider()
    e = engine(tmp_path, provider, system_prompt='Custom benchmark profile')
    collect(e)
    prompt = provider.prompts[0]
    assert 'Custom benchmark profile' in prompt
    for phrase in ('tests, fixtures, expected outputs', 'archive member roots',
                   'legacy config filenames, shebangs', 'target interpreter',
                   'Charon\'s own', 'byte-identical', 'actually running the check'):
        assert phrase in prompt


def test_noninteractive_clarify_absent_and_cannot_be_reenabled_or_called(tmp_path, monkeypatch):
    monkeypatch.setenv('CHARON_NON_INTERACTIVE', '1')
    provider = Provider()
    e = engine(tmp_path, provider)
    collect(e)
    assert 'Clarify' not in {t['name'] for t in provider.tools}
    assert e._enable_tools(['Clarify']) == []
    assert e._needs_browser_prompt() is False
    assert 'explicitly record a reasonable' in provider.prompts[0]
    result = execute_clarify({'action': 'ask', 'question': 'Credentials?'}, e.tool_context)
    assert result.is_error and 'unavailable' in result.content
    assert not list(tmp_path.rglob('clarifications.json'))
    # Explicit interactive mode still exposes the tool, even with the env default.
    interactive = engine(tmp_path, Provider(), interactive=True)
    assert 'Clarify' in {t['name'] for t in interactive.tools}


def test_error_loop_warns_then_stops_preserving_partial_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv('CHARON_CONSECUTIVE_TOOL_ERRORS', '2')
    from charon.conversation import conversation_engine as module

    def fail(name, args, ctx):
        (ctx.project_root / 'partial.txt').write_text('best partial result')
        return ToolResult(content='command not found', is_error=True)

    monkeypatch.setattr(module, 'execute_tool', fail)
    provider = Provider('Bash')
    e = engine(tmp_path, provider, interactive=False)
    events = collect(e)
    assert provider.calls == 4
    assert sum(event.type == 'strategy_change' for event in events) == 1
    assert sum(event.type == 'budget_stop' for event in events) == 1
    assert 'Change strategy now' in provider.prompts[-1] or any(
        'Change strategy now' in message.content for message in e.messages)
    assert events[-1].data['stop_reason'] == 'budget_exhausted'
    assert len([m for m in e.messages if m.role == 'tool_result']) == 4
    assert 'incomplete' in e.messages[-1].content
    assert (tmp_path / 'partial.txt').read_text() == 'best partial result'


def test_stagnation_tracks_repeated_observations_not_argument_variations():
    guard = LoopGuard(error_limit=3, stagnant_limit=2)
    assert guard.observe('Bash', ToolResult(content='same')) is None
    assert guard.observe('Bash', ToolResult(content='same')) is None
    assert guard.observe('Bash', ToolResult(content='same'))[0] == 'warn'
    assert guard.observe('Read', ToolResult(content='new evidence')) is None
    assert guard.observe('Bash', ToolResult(content='same', details={'state_changed': True})) is None
    assert guard.observe('Bash', ToolResult(content='same')) is None
    assert guard.observe('Bash', ToolResult(content='same'))[0] == 'stop'


def test_deadline_cancels_waiting_provider_and_records_incomplete(tmp_path, monkeypatch):
    from charon.infra import config
    monkeypatch.setattr(config, 'task_time_budget_seconds', lambda: 0.02)

    class Waiting(Provider):
        closed = False

        async def stream(self, **kwargs):
            try:
                await asyncio.sleep(60)
                yield StreamDelta(type='text', text='should not arrive')
            finally:
                self.closed = True

    provider = Waiting()
    e = engine(tmp_path, provider)
    events = collect(e)
    assert provider.closed
    assert any(event.type == 'budget_stop' for event in events)
    assert 'task time budget exhausted' in e.messages[-1].content


def test_deadline_signals_running_tool_and_keeps_protocol_valid(tmp_path, monkeypatch):
    from charon.conversation import conversation_engine as module
    from charon.infra import config
    monkeypatch.setattr(config, 'task_time_budget_seconds', lambda: 0.02)

    def slow(name, args, ctx):
        assert ctx.cancel_event.wait(timeout=2)
        return ToolResult(content='cancelled', is_error=True)

    monkeypatch.setattr(module, 'execute_tool', slow)
    e = engine(tmp_path, Provider('Bash'))
    events = collect(e)
    assert any(event.type == 'budget_stop' for event in events)
    assert len([m for m in e.messages if m.role == 'tool_result']) == 1
    assert e._turn_cancel_event.is_set()


def test_direct_noninteractive_clarify_does_not_create_state(tmp_path):
    ctx = ToolContext(project_root=tmp_path, state_dir=tmp_path / 'state',
                      metadata={'interactive': False})
    assert execute_clarify({'action': 'ask', 'question': 'Which one?'}, ctx).is_error
    assert not (tmp_path / 'state').exists()


def test_one_shot_budget_stop_is_failure_even_with_partial_text():
    import importlib.util
    from pathlib import Path
    from charon.conversation.conversation_engine import EngineEvent

    path = Path(__file__).resolve().parents[1] / 'scripts/charon_chat.py'
    spec = importlib.util.spec_from_file_location('task_policy_chat', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Stopped:
        async def submit(self, query):
            yield EngineEvent(type='text_delta', data={'text': 'Partial artifact saved'})
            yield EngineEvent(type='budget_stop', data={'reason': 'deadline'})
            yield EngineEvent(type='done', data={})

    assert asyncio.run(module.one_shot(Stopped(), 'task')) == 1
