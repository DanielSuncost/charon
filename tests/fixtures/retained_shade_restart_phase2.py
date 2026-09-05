"""Phase 2 helper for test_retained_shade_restart.py.

Run as a standalone subprocess (not imported), in a brand-new OS process
sharing nothing with retained_shade_restart_phase1.py but the state_dir on
disk (module-level caches like agent_runtime's engine cache, PyKernel's
kernel registry, and store_adapter's DB-handle cache all start empty here,
exactly like a real daemon restart). Reactivates the shade phase 1 left
idle via the real execute_reactivate_shade path, and reports whether the
secret only known from phase 1's conversation actually gets sent to the
model on this new turn -- i.e. whether load_from_store() really recovered
it from disk, not just that the reactivation call itself succeeded.

Usage: python retained_shade_restart_phase2.py <state_dir> <shade_id> <secret>
Prints one JSON line: {"state_before_reactivation", "state_after_reactivation",
"contract_status", "sent_message_count", "secret_was_sent_to_model"}.
"""
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / 'src'))

from charon.tools import ToolContext  # noqa: E402
from charon.tools.shade_tool import execute_reactivate_shade  # noqa: E402
from charon.shade.shade_orchestrator import get_contract  # noqa: E402
from charon.agents import shade_lifecycle  # noqa: E402
from charon.agents.topology_budget import mint_budget  # noqa: E402
from charon.providers import StreamDelta, ModelInfo  # noqa: E402

STATE_DIR = Path(sys.argv[1])
SHADE_ID = sys.argv[2]
SECRET = sys.argv[3]

_captured = {'messages': None}


class _CapturingProvider:
    async def stream(self, messages, model, system_prompt, tools=None, thinking_level='off', max_tokens=16384):
        _captured['messages'] = [{'role': m.role, 'content': m.content} for m in messages]
        yield StreamDelta(type='text', text='Confirmed: still have the pre-restart checkpoint in context.')
        yield StreamDelta(type='done', text=json.dumps({
            'usage': {'input_tokens': 40, 'output_tokens': 20, 'total_tokens': 60},
            'stop_reason': 'end_turn',
        }))


def _fake_provider_resolver(*a, **k):
    return (_CapturingProvider(), ModelInfo(provider='fake-provider', model_id='fake-model', context_window=200000), {})


def _wait_for_contract(contract_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = get_contract(STATE_DIR, contract_id)
        if c and c.get('status') in ('completed', 'failed'):
            return c
        time.sleep(0.02)
    raise TimeoutError(contract_id)


def main():
    state_before = shade_lifecycle.get_state(STATE_DIR, SHADE_ID)

    with patch(
        'charon.providers.worker_provider.ensure_worker_provider_or_request_clarification',
        lambda *a, **k: {'ok': True},
    ), patch(
        'charon.providers.model_registry.get_shade_provider_and_model',
        _fake_provider_resolver,
    ):
        ctx = ToolContext(project_root=_REPO_ROOT, state_dir=STATE_DIR, agent_id='AG-ROOT')
        budget = mint_budget('AG-ROOT', preset='standard')
        handle = execute_reactivate_shade(
            STATE_DIR, SHADE_ID,
            'Do you still remember the checkpoint word from before the restart? State it.',
            ctx, depth=1, budget=budget,
        )
        contract = _wait_for_contract(handle['contract_id'])

    sent = _captured['messages'] or []
    print(json.dumps({
        'state_before_reactivation': state_before,
        'state_after_reactivation': shade_lifecycle.get_state(STATE_DIR, SHADE_ID),
        'contract_status': contract.get('status'),
        'sent_message_count': len(sent),
        'secret_was_sent_to_model': SECRET in json.dumps(sent),
    }))


if __name__ == '__main__':
    main()
