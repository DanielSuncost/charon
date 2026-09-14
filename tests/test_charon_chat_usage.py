from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'charon_chat.py'


def _load_chat_module():
    spec = importlib.util.spec_from_file_location('charon_chat_usage_test', SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeEngine:
    async def submit(self, query: str):
        assert query == 'test query'
        yield SimpleNamespace(type='tool_call', data={'tool_name': 'Read', 'arguments': {'path': 'x'}})
        yield SimpleNamespace(
            type='message_end',
            data={
                'usage': {
                    'input_tokens': 100,
                    'output_tokens': 20,
                    'total_tokens': 120,
                    'cache_read_tokens': 40,
                }
            },
        )
        yield SimpleNamespace(type='turn_end', data={'stop_reason': 'tool_use'})
        yield SimpleNamespace(type='text_delta', data={'text': 'clean reply'})
        yield SimpleNamespace(
            type='message_end',
            data={
                'usage': {
                    'input_tokens': 125,
                    'output_tokens': 5,
                    'total_tokens': 130,
                    'cache_read_tokens': 10,
                    'cache_write_tokens': 3,
                }
            },
        )
        yield SimpleNamespace(type='turn_end', data={'stop_reason': 'end_turn'})
        yield SimpleNamespace(
            type='done',
            data={'total_turns': 2, 'message_count': 4, 'pending_follow_ups': 0},
        )


def test_one_shot_emits_aggregate_usage_only_on_stderr(capsys):
    chat = _load_chat_module()

    exit_code = asyncio.run(chat.one_shot(FakeEngine(), 'test query'))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == 'clean reply\n'

    stderr_lines = captured.err.splitlines()
    assert stderr_lines[0].startswith('[tool] Read ')
    assert json.loads(stderr_lines[-1]) == {
        'usage': {
            'input_tokens': 225,
            'output_tokens': 25,
            'cache_read_tokens': 50,
            'cache_write_tokens': 3,
            'total_tokens': 250,
        }
    }
    assert sum(line.startswith('{"usage":') for line in stderr_lines) == 1
