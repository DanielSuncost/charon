"""Live: the Codex backend accepts every effort level advertised for gpt-6-astra.

Opt-in only — set CHARON_LIVE_CODEX=1 and CHARON_LIVE_CODEX_STATE_DIR to a
Charon state dir whose auth/auth.json holds an openai-codex token. The stored
access token is used as-is (no refresh token is passed, so nothing is written
back). Credentials are never printed; anything token-shaped in an error is
redacted. A real HTTP 401 fails the current level and skips the rest instead
of retrying.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import pytest

from charon.providers import Message, ModelInfo
from charon.providers.effort import supported_efforts
from charon.providers.httpx_codex import HttpxCodexProvider

MODEL = 'gpt-6-astra'
# Every wire level astra accepts, plus 'ultra' — which is sent as 'max' and must
# still be accepted (the endpoint rejects the literal 'ultra' with a 400, and
# 'minimal' with another; both were observed while building the table).
LEVELS = tuple(supported_efforts(MODEL)) + ('ultra',)
_halt: dict[str, str] = {}


def _redact(text: str) -> str:
    return re.sub(r'[A-Za-z0-9_\-.]{32,}', '[redacted]', text or '')


def _token() -> str:
    if os.environ.get('CHARON_LIVE_CODEX') != '1':
        pytest.skip('set CHARON_LIVE_CODEX=1 to run the live Codex effort check')
    state_dir = os.environ.get('CHARON_LIVE_CODEX_STATE_DIR', '').strip()
    auth = Path(state_dir) / 'auth' / 'auth.json' if state_dir else None
    if auth is None or not auth.exists():
        pytest.skip('CHARON_LIVE_CODEX_STATE_DIR must point at a state dir with auth/auth.json')
    token = json.loads(auth.read_text()).get('providers', {}).get('openai-codex', {}).get('tokens', {}).get('access_token')
    if not token:
        pytest.skip('no openai-codex access token in that auth store')
    return token


async def _ask(provider: HttpxCodexProvider, level: str):
    text, errors, thinking = [], [], 0
    try:
        async for d in provider.stream(
            messages=[Message(role='user', content='Reply with exactly the word OK.')],
            model=ModelInfo(provider='codex', model_id=MODEL, context_window=1_050_000, supports_thinking=True),
            system_prompt='You are terse.',
            thinking_level=level,
            max_tokens=64,
        ):
            if d.type == 'text':
                text.append(d.text)
            elif d.type == 'thinking':
                thinking += 1
            elif d.type == 'error':
                errors.append(str(d.error or ''))
    finally:
        aclose = getattr(provider, 'aclose', None)
        if callable(aclose):
            await aclose()
    return ''.join(text), errors, thinking


@pytest.mark.parametrize('level', LEVELS)
def test_codex_accepts_each_astra_effort_level_live(level):
    if _halt:
        pytest.skip(_halt['reason'])
    provider = HttpxCodexProvider(api_key=_token())
    started = time.perf_counter()
    text, errors, thinking = asyncio.run(_ask(provider, level))
    elapsed = time.perf_counter() - started
    transport = (provider.last_request_metrics or {}).get('transport', '?')
    print(f'\n[live] {MODEL} effort={level:<6} transport={transport:<9} {elapsed:5.1f}s '
          f'thinking_deltas={thinking} text={_redact(text.strip())[:40]!r} errors={[_redact(e)[:120] for e in errors]}')
    if any('401' in e for e in errors):
        _halt['reason'] = 'HTTP 401 from Codex — stopped, not retrying'
        pytest.fail(_halt['reason'])
    assert not errors, f'level {level} rejected: {[_redact(e)[:160] for e in errors]}'
    assert text.strip(), f'level {level} returned no text'
