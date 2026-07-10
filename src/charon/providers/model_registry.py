"""Model registry — manages available models and tier assignments for shades.

Three tiers:
  fast   — cheap/fast models for analysis, verification, summarization
  strong — capable models for implementation, complex reasoning  
  auto   — let Charon pick based on task complexity

Configuration stored in .charon_state/model_registry.json
Also configurable via /setup shade-model and onboarding.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from charon.infra import config


DEFAULT_REGISTRY = {
    'shade_model_mode': 'auto',  # 'auto' (pick per task), 'same' (use main model), 'fixed'
    'shade_model': None,         # specific model id when mode='fixed'
    'shade_provider': None,      # specific provider when mode='fixed'
    'shade_base_url': None,      # custom base URL (e.g., openrouter)
    'shade_api_key': None,       # API key for shade provider
    'tiers': {
        'fast': None,            # model_id or None (falls back to main model)
        'strong': None,
    },
    'phase_tier_map': {
        'analysis': 'fast',
        'planning': 'strong',
        'implementation': 'strong',
        'verification': 'fast',
        'report': 'fast',
        'research': 'fast',
        'generation': 'fast',    # batch work like image generation
    },
}

# Shared provider instances — avoids OAuth refresh token races
# when multiple shades launch simultaneously
_provider_lock = threading.Lock()
_shared_main_provider = None
_shared_main_model = None
_shared_main_ready = None
_shared_shade_provider = None
_shared_shade_model = None
_shared_shade_ready = None


def load_registry(state_dir: Path) -> dict:
    """Load model registry config."""
    reg = dict(DEFAULT_REGISTRY)
    reg['tiers'] = dict(DEFAULT_REGISTRY['tiers'])
    reg['phase_tier_map'] = dict(DEFAULT_REGISTRY['phase_tier_map'])

    try:
        p = state_dir / 'model_registry.json'
        if p.exists():
            user = json.loads(p.read_text())
            if isinstance(user, dict):
                for k in ('shade_model_mode', 'shade_model', 'shade_provider',
                           'shade_base_url', 'shade_api_key'):
                    if k in user:
                        reg[k] = user[k]
                if 'tiers' in user and isinstance(user['tiers'], dict):
                    reg['tiers'].update(user['tiers'])
                if 'phase_tier_map' in user and isinstance(user['phase_tier_map'], dict):
                    reg['phase_tier_map'].update(user['phase_tier_map'])
    except Exception:
        pass

    # Env overrides
    env_mode = config.shade_model_mode()
    if env_mode:
        reg['shade_model_mode'] = env_mode
    env_model = config.shade_model()
    if env_model:
        reg['shade_model'] = env_model
        reg['shade_model_mode'] = 'fixed'

    return reg


def save_registry(state_dir: Path, reg: dict) -> None:
    p = state_dir / 'model_registry.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2))


def _get_shared_main(state_dir: Path):
    """Get or create the shared main provider (thread-safe)."""
    global _shared_main_provider, _shared_main_model, _shared_main_ready
    with _provider_lock:
        if _shared_main_provider is None:
            from charon.providers.provider_bridge import create_provider_and_model
            _shared_main_provider, _shared_main_model, _shared_main_ready = create_provider_and_model(state_dir)
        return _shared_main_provider, _shared_main_model, _shared_main_ready


def _get_shared_shade(state_dir: Path, reg: dict):
    """Get or create the shared shade-specific provider (thread-safe)."""
    global _shared_shade_provider, _shared_shade_model, _shared_shade_ready
    with _provider_lock:
        if _shared_shade_provider is not None:
            return _shared_shade_provider, _shared_shade_model, _shared_shade_ready

        from charon.providers.provider_bridge import CONTEXT_WINDOWS, DEFAULT_CONTEXT_WINDOW
        from charon.providers import ModelInfo

        shade_model = reg.get('shade_model')
        shade_provider_name = reg.get('shade_provider') or 'local'

        model = ModelInfo(
            provider=shade_provider_name,
            model_id=shade_model,
            context_window=CONTEXT_WINDOWS.get(shade_model, DEFAULT_CONTEXT_WINDOW),
            supports_thinking=False,
        )

        base_url = reg.get('shade_base_url')
        api_key = reg.get('shade_api_key') or os.environ.get('OPENROUTER_API_KEY', '')

        if shade_provider_name == 'anthropic':
            from charon.providers.anthropic import AnthropicProvider
            provider = AnthropicProvider(api_key=api_key)
        else:
            from charon.providers.httpx_openai import HttpxOpenAIProvider
            provider = HttpxOpenAIProvider(
                base_url=base_url or 'http://127.0.0.1:1234/v1',
                api_key=api_key or 'not-needed',
            )

        is_local = shade_provider_name in ('local', 'lmstudio', 'ollama')
        _shared_shade_provider = provider
        _shared_shade_model = model
        _shared_shade_ready = bool(api_key) or is_local
        return _shared_shade_provider, _shared_shade_model, _shared_shade_ready


def get_shade_provider_and_model(
    state_dir: Path,
    *,
    phase_name: str = '',
    task_complexity: str = 'normal',
):
    """Resolve which provider+model a shade should use.

    Returns (provider, model_info, ready).
    All shades share provider instances to avoid OAuth refresh races.
    """
    reg = load_registry(state_dir)
    mode = reg.get('shade_model_mode', 'auto')

    if mode == 'same':
        return _get_shared_main(state_dir)

    if mode == 'fixed':
        shade_model = reg.get('shade_model')
        if not shade_model:
            return _get_shared_main(state_dir)
        return _get_shared_shade(state_dir, reg)

    if mode == 'auto':
        tier_map = reg.get('phase_tier_map', {})
        tier = tier_map.get(phase_name, 'strong' if task_complexity == 'complex' else 'fast')
        tier_model = (reg.get('tiers') or {}).get(tier)

        if tier_model:
            # Use the tier-specific model via the shade provider
            reg_copy = dict(reg)
            reg_copy['shade_model'] = tier_model
            return _get_shared_shade(state_dir, reg_copy)

        # No tier model configured — use main provider
        return _get_shared_main(state_dir)

    # Unknown mode
    return _get_shared_main(state_dir)
