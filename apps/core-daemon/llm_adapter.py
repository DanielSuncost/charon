#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / '.charon_state'
DEFAULT_MODEL = 'qwen3.5-27b-uncensored-hauhaucs-aggressive'
DEFAULT_BASE_URL = os.environ.get('CHARON_LMSTUDIO_BASE_URL', 'http://127.0.0.1:1234/v1')


def _load_onboarding() -> dict:
    path = STATE_DIR / 'onboarding.json'
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _detect_model_from_onboarding() -> str | None:
    onboarding = _load_onboarding()
    for key in ('provider_model', 'opencode_model', 'model'):
        val = str(onboarding.get(key) or '').strip()
        if val:
            return val.split('/', 1)[1] if val.startswith('lmstudio/') else val
    return None


def detect_base_url() -> str:
    env = os.environ.get('CHARON_LMSTUDIO_BASE_URL', '').strip()
    if env:
        return env.rstrip('/')

    onboarding = _load_onboarding()
    base = str(onboarding.get('provider_base_url') or '').strip()
    if base:
        return base.rstrip('/')

    return DEFAULT_BASE_URL.rstrip('/')


def _detect_model_from_user_state() -> str | None:
    model_file = ROOT / '.charon_state' / 'user_model.json'
    if not model_file.exists():
        return None
    try:
        data = json.loads(model_file.read_text())
    except Exception:
        return None
    prefs = data.get('preferences', {}) or {}
    for key in ('local_model', 'default_model', 'model'):
        val = ((prefs.get(key) or {}).get('value') or '').strip()
        if val:
            return val.split('/', 1)[1] if val.startswith('lmstudio/') else val
    return None


def detect_model() -> str:
    env = os.environ.get('CHARON_LOCAL_MODEL', '').strip()
    if env:
        return env.split('/', 1)[1] if env.startswith('lmstudio/') else env

    preferred = _detect_model_from_user_state()
    if preferred:
        return preferred

    onboarding_model = _detect_model_from_onboarding()
    if onboarding_model:
        return onboarding_model

    cfg = Path.home() / '.config' / 'opencode' / 'opencode.json'
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text())
            providers = data.get('provider', {}) if isinstance(data, dict) else {}
            lmstudio_models = ((providers.get('lmstudio', {}) or {}).get('models', {}) or {}) if isinstance(providers, dict) else {}
            if lmstudio_models:
                return next(iter(lmstudio_models.keys()))
        except Exception:
            pass
    return DEFAULT_MODEL

def _strip_thinking(text: str) -> str:
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE).strip()

    # Heuristic cleanup for reasoning-model leakage.
    lowered = text.lower()
    if lowered.startswith('thinking process') or lowered.startswith('thinking process:'):
        parts = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
        if parts:
            # usually final answer is in the last paragraph
            text = parts[-1]

    # remove common prefixed meta lines
    text = re.sub(r'^(thinking\s*process|reasoning|analysis)\s*:\s*', '', text, flags=re.IGNORECASE)

    # If multiline and still contains bullets/steps, return last non-empty line
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) > 1:
        if any(lines[0].lower().startswith(k) for k in ('1.', '-', '*', 'step', 'thinking')):
            text = lines[-1]

    return text.strip()

def _post_json(url: str, payload: dict, timeout: int = 240) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer lm-studio'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8', errors='replace'))

def query_local_model(prompt: str, model: str | None = None, timeout: int = 240, cwd: str | None = None) -> tuple[bool, str]:
    _ = cwd
    model_id = model or detect_model()
    if model_id.startswith('lmstudio/'):
        model_id = model_id.split('/', 1)[1]

    system = (
        'You are Charon, a practical engineering assistant. '
        'Be concise, direct, and actionable. '
        'Never output chain-of-thought, <think> tags, or hidden reasoning. '
        'Respond with final answer only. '
        'When uncertain, say so explicitly.'
    )
    payload = {
        'model': model_id,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': prompt},
        ],
        'temperature': 0.3,
        'max_tokens': 700,
    }

    base_url = detect_base_url()

    try:
        data = _post_json(f'{base_url}/chat/completions', payload, timeout=timeout)
        choices = data.get('choices') or []
        if not choices:
            return False, 'no response choices from local model'
        msg = (choices[0].get('message') or {}).get('content', '')
        text = _strip_thinking((msg or '').strip())
        return (True, text) if text else (False, 'empty response from local model')
    except Exception as e:
        return False, f'local model request failed: {e}'
