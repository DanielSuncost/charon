"""Phase 1 helper for test_retained_shade_restart.py.

Run as a standalone subprocess (not imported): spawns a RETAINED shade via
the real execute_spawn_shade -> _run_shade -> ConversationEngine path (only
the model provider is faked) and lets it go idle, then exits completely.
The test harness runs this as a genuinely separate OS process so nothing
here shares Python memory with retained_shade_restart_phase2.py -- only the
state_dir on disk is shared, the same way two runs of a real daemon would
share nothing but disk across a restart.

Usage: python retained_shade_restart_phase1.py <state_dir> <secret>
Prints one JSON line: {"shade_id", "contract_status", "lifecycle_state"}.
"""
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / 'src'))

from charon.tools import ToolContext  # noqa: E402
from charon.tools.shade_tool import execute_spawn_shade  # noqa: E402
from charon.shade.shade_orchestrator import get_contract  # noqa: E402
from charon.agents import shade_lifecycle  # noqa: E402
from charon.providers import StreamDelta, ModelInfo  # noqa: E402

STATE_DIR = Path(sys.argv[1])
SECRET = sys.argv[2]


class _FakeProvider:
    def __init__(self, text):
        self._text = text

    async def stream(self, messages, model, system_prompt, tools=None, thinking_level='off', max_tokens=16384):
        yield StreamDelta(type='text', text=self._text)
        yield StreamDelta(type='done', text=json.dumps({
            'usage': {'input_tokens': 120, 'output_tokens': 60, 'total_tokens': 180},
            'stop_reason': 'end_turn',
        }))


def _fake_provider_resolver(*a, **k):
    text = (
        f'Investigated the intermittent auth failures. Root cause not yet confirmed. '
        f'Checkpoint for continuation after restart: the verification word is {SECRET}.'
    )
    return (_FakeProvider(text), ModelInfo(provider='fake-provider', model_id='fake-model', context_window=200000), {})


def _wait_for_contract(contract_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = get_contract(STATE_DIR, contract_id)
        if c and c.get('status') in ('completed', 'failed'):
            return c
        time.sleep(0.02)
    raise TimeoutError(contract_id)


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with patch(
        'charon.providers.worker_provider.ensure_worker_provider_or_request_clarification',
        lambda *a, **k: {'ok': True},
    ), patch(
        'charon.providers.model_registry.get_shade_provider_and_model',
        _fake_provider_resolver,
    ):
        ctx = ToolContext(project_root=_REPO_ROOT, state_dir=STATE_DIR, agent_id='AG-ROOT')
        result = execute_spawn_shade({
            'goal': 'Investigate intermittent auth failures; note a checkpoint before pausing',
            'retain': True,
            'phase_specs': [{'name': 'investigate', 'objective': 'Investigate and note a checkpoint word'}],
        }, ctx)
        if result.is_error:
            print(json.dumps({'error': result.content}))
            sys.exit(1)
        shade_id = result.details['shade_id']
        contract = _wait_for_contract(result.details['contract_id'])

    print(json.dumps({
        'shade_id': shade_id,
        'contract_status': contract.get('status'),
        'lifecycle_state': shade_lifecycle.get_state(STATE_DIR, shade_id),
    }))


if __name__ == '__main__':
    main()
