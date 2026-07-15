#!/usr/bin/env python3
"""IPMS smoke: live 2-turn swap proof.

Turn 1 on model A → full-fidelity checkpoint → silent resume on a fresh
engine (model B) → turn 2 must recall a turn-1 fact that exists nowhere in
the visible prompt. With one authenticated provider this runs as swap-same
(the IS ceiling condition); pass --target-provider/--target-model when a
second backbone is available.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from charon.context.context_transfer import (  # noqa: E402
    apply_checkpoint_to_engine, create_checkpoint_from_engine,
)
from charon.conversation.conversation_engine import ConversationEngine  # noqa: E402
from charon.providers.provider_bridge import create_provider_and_model  # noqa: E402

SYSTEM = (
    'You are Nyx, a persistent release engineer for the Charon project. '
    'You keep your commitments across sessions and answer concisely.'
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--auth-state-dir', default=str(ROOT / '.charon_state'),
                    help='state dir holding onboarding.json + auth (provider construction only)')
    ap.add_argument('--scratch', default=str(ROOT / '.ipms_smoke'),
                    help='scratch dir for checkpoint artifacts')
    ap.add_argument('--model-a', default='gpt-5.4',
                    help='backbone for turn 1 (must be accepted by the authenticated provider)')
    ap.add_argument('--model-b', default='gpt-5.5',
                    help='backbone for turn 2 after the swap')
    args = ap.parse_args()

    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)

    provider, base_model, ready = create_provider_and_model(Path(args.auth_state_dir))
    if not ready:
        print('no ready provider (check onboarding.json / auth)', file=sys.stderr)
        return 2

    from charon.providers import ModelInfo
    model_a = ModelInfo(provider=base_model.provider, model_id=args.model_a,
                        context_window=base_model.context_window)
    model_b = ModelInfo(provider=base_model.provider, model_id=args.model_b,
                        context_window=base_model.context_window)

    fact = f'ipms-{int(time.time()) % 100000}'

    engine_a = ConversationEngine(provider, model_a, system_prompt=SYSTEM,
                                  thinking_level='off', auto_compact=False,
                                  max_tokens=512)
    engine_a.tools = []
    turn1, _ = asyncio.run(engine_a.submit_and_collect(
        f'Remember this for later: the canary codeword is "{fact}". '
        'Acknowledge in one short sentence without repeating the codeword.'))

    ckpt = create_checkpoint_from_engine(engine_a, state_dir=scratch,
                                         label='smoke-turn-1')

    engine_b = ConversationEngine(provider, model_b, system_prompt='(placeholder)',
                                  thinking_level='off', auto_compact=False,
                                  max_tokens=512)
    engine_b.tools = []
    restored = apply_checkpoint_to_engine(engine_b, ckpt)
    turn2, _ = asyncio.run(engine_b.submit_and_collect(
        'What is the canary codeword? Reply with just the codeword.'))

    recalled = fact in turn2
    print(json.dumps({
        'provider': getattr(engine_b, 'provider_name', ''),
        'model_a': model_a.model_id,
        'model_b': model_b.model_id,
        'checkpoint_id': ckpt['id'],
        'restored_messages': restored,
        'fact': fact,
        'turn1_reply': turn1.strip()[:200],
        'turn2_reply': turn2.strip()[:200],
        'turn1_leaked_fact': fact in turn1,
        'recalled_across_swap': recalled,
    }, indent=2))
    return 0 if recalled else 1


if __name__ == '__main__':
    sys.exit(main())
