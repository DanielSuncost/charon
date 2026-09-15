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

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


# ── billing basis ────────────────────────────────────────────────────────────
# A per-token dollar estimate is only a real cost under metered API-key billing.
# OAuth/subscription providers (codex, claude-code) are flat-rate — a per-token $
# is fictional — and local providers are free. Consumers use the billing mode to
# suppress or clearly label the estimated_cost_usd figure instead of presenting a
# made-up dollar amount as if it were billed.
SUBSCRIPTION_PROVIDERS = frozenset({'codex', 'claude-code'})
LOCAL_PROVIDERS = frozenset({'lmstudio', 'local'})


def provider_billing_mode(provider: str = '', auth: str = '') -> str:
    """Classify a provider/auth pair as 'metered' (real per-token $), 'subscription'
    (OAuth flat-rate — $ is notional) or 'local' (free). Unknown defaults to
    'metered': safer to show a number than to silently hide a genuine cost."""
    p = (provider or '').strip().lower()
    a = (auth or '').strip().lower()
    if p in LOCAL_PROVIDERS:
        return 'local'
    if a == 'oauth' or p in SUBSCRIPTION_PROVIDERS:
        return 'subscription'
    return 'metered'


def cost_is_real(billing_mode: str) -> bool:
    """True only when a dollar figure reflects actual metered billing."""
    return (billing_mode or '').strip().lower() == 'metered'


def resolve_billing_mode(state_dir: Path) -> str:
    """Best-effort billing basis for the active provider. Prefers the fixed shade
    provider (what background/Libris runs actually use), else the onboarding
    provider + auth. Defaults to 'metered' when nothing is resolvable."""
    try:
        reg = load_registry(state_dir)
        sp = str(reg.get('shade_provider') or '').strip().lower()
        if sp:
            mode = provider_billing_mode(sp, 'oauth' if sp in SUBSCRIPTION_PROVIDERS else '')
            if mode != 'metered':
                return mode
    except Exception:
        pass
    try:
        ob = json.loads((Path(state_dir) / 'onboarding.json').read_text(encoding='utf-8'))
        return provider_billing_mode(str(ob.get('provider') or ''), str(ob.get('provider_auth') or ''))
    except Exception:
        return 'metered'


DEFAULT_REGISTRY = {
    'shade_model_mode': 'auto',  # 'auto' (pick per task), 'same' (use main model), 'fixed'
    'shade_effort_mode': 'auto', # 'auto' (by task_complexity, degrading with budget), 'same' (inherit the session's level)
    'effort_by_complexity': {    # worker reasoning effort per task_complexity (mode 'auto')
        'simple': 'low',         # a probe or a lookup should not burn deep reasoning
        'normal': 'medium',      # the Codex backend's own default for most models
        'complex': 'high',       # xhigh/max/ultra are opt-in here: they multiply per-worker latency across a tree
    },
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
    reg['effort_by_complexity'] = dict(DEFAULT_REGISTRY['effort_by_complexity'])

    try:
        p = state_dir / 'model_registry.json'
        if p.exists():
            user = json.loads(p.read_text())
            if isinstance(user, dict):
                for k in ('shade_model_mode', 'shade_effort_mode', 'shade_model', 'shade_provider',
                           'shade_base_url', 'shade_api_key'):
                    if k in user:
                        reg[k] = user[k]
                if 'effort_by_complexity' in user and isinstance(user['effort_by_complexity'], dict):
                    reg['effort_by_complexity'].update(user['effort_by_complexity'])
                if 'tiers' in user and isinstance(user['tiers'], dict):
                    reg['tiers'].update(user['tiers'])
                if 'phase_tier_map' in user and isinstance(user['phase_tier_map'], dict):
                    reg['phase_tier_map'].update(user['phase_tier_map'])
    except Exception as e:
        _diag('model_registry', 'model_registry.json load failed; using defaults', error=e)

    # Env overrides
    env_mode = config.shade_model_mode()
    if env_mode:
        reg['shade_model_mode'] = env_mode
    env_model = config.shade_model()
    if env_model:
        reg['shade_model'] = env_model
        reg['shade_model_mode'] = 'fixed'
    env_effort_mode = config.shade_effort_mode()
    if env_effort_mode:
        reg['shade_effort_mode'] = env_effort_mode

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


SCARCE_UTILIZATION = 0.75


def _budget_scarce(state_dir: Path, budget: dict | None) -> bool:
    """True once this delegation tree has used >= 75% of its token or cost budget.
    The one signal both routers degrade on, so tier and effort step down together."""
    if not budget:
        return False
    from charon.agents.topology_budget import token_budget_utilization, cost_budget_utilization
    seen = [u for u in (token_budget_utilization(state_dir, budget), cost_budget_utilization(state_dir, budget)) if u is not None]
    return bool(seen) and max(seen) >= SCARCE_UTILIZATION


def _session_thinking_level(state_dir: Path) -> str:
    """The user's own level from onboarding.json ('off' when unset)."""
    from charon.providers.effort import normalize_thinking_level
    try:
        data = json.loads((Path(state_dir) / 'onboarding.json').read_text(encoding='utf-8'))
        return normalize_thinking_level(data.get('reasoning_effort') or data.get('thinking_level'))
    except Exception:
        return 'off'


def route_shade_effort(
    state_dir: Path,
    *,
    task_complexity: str = 'normal',
    budget: dict | None = None,
    user_level: str | None = None,
) -> str:
    """Pick a worker's reasoning effort the way route_shade_model picks a tier.

    Mode 'auto' (default): `effort_by_complexity[task_complexity]` from the
    registry — simple→low, normal→medium, complex→high unless overridden —
    stepped down one notch (never below 'low') once the tree's budget is
    scarce, the same >=75% signal that moves the model tier. A cheap probe
    therefore never runs at high, and a hard contract under a starved budget
    still gets medium rather than being dropped to the floor.

    Mode 'same': the session's own level (`user_level`, else onboarding.json),
    for users who want workers to reason exactly as they do.

    This is worker policy only: the user's own session keeps whatever they
    chose. Never raises — any failure returns the plain complexity default.
    """
    from charon.providers.effort import normalize_thinking_level, step_down
    try:
        reg = load_registry(state_dir)
        mode = str(reg.get('shade_effort_mode') or 'auto').strip().lower()
        if mode == 'same':
            if user_level is not None:
                return normalize_thinking_level(user_level)
            return _session_thinking_level(state_dir)
        table = dict(DEFAULT_REGISTRY['effort_by_complexity'])
        table.update(reg.get('effort_by_complexity') or {})
        complexity = str(task_complexity or 'normal').strip().lower()
        level = normalize_thinking_level(table.get(complexity, table['normal']), default='medium')
        if _budget_scarce(state_dir, budget):
            level = step_down(level, floor='low')
        return level
    except Exception as e:
        _diag('model_registry', 'shade effort routing failed; using the complexity default', error=e)
        return {'simple': 'low', 'complex': 'high'}.get(str(task_complexity or '').lower(), 'medium')


def route_shade_model(state_dir: Path, *, task_complexity: str = 'normal', budget: dict | None = None) -> str:
    """Rank 'fast' vs 'strong' via the real multi-objective router
    (charon.routing.policy) instead of the plain complex-or-not switch,
    picking the ECONOMY policy once this delegation tree's budget is
    running low and BALANCED otherwise. Returns a task_complexity string
    ('normal' or 'complex') for the caller to pass to
    get_shade_provider_and_model as usual — this never resolves a
    provider/model itself, so it can't fight that function's own OAuth-
    reuse caching.

    Falls back to `task_complexity` unchanged (today's behavior, zero
    regression) whenever:
    - fewer than 2 distinct real tier models are configured (the common
      default setup — routing needs real choices to rank between), or
    - billing is 'subscription' or 'local' (provider_billing_mode) — under
      flat-rate/free billing, a real dollar difference between tiers
      doesn't exist, so cost-aware routing would trade away quality for a
      cost saving that isn't real, or
    - routing raises for any reason.
    """
    try:
        reg = load_registry(state_dir)
        tiers = reg.get('tiers') or {}
        fast_model = str(tiers.get('fast') or '').strip()
        strong_model = str(tiers.get('strong') or '').strip()
        if not fast_model or not strong_model or fast_model == strong_model:
            return task_complexity
        if not cost_is_real(resolve_billing_mode(state_dir)):
            return task_complexity

        from charon.routing.policy import route_models, ModelCandidate, TaskProfile, PolicyName
        from charon.infra.orchestration_trace import estimate_cost_usd

        # A nominal call's input+output size, purely to compare the two
        # tiers' *relative* cost — not a claim about any real call's size.
        representative_tokens = 2000
        candidates = [
            ModelCandidate(
                candidate_id='fast', provider='shade', model_id=fast_model,
                capabilities=(), context_window=0,
                estimated_cost_usd=estimate_cost_usd(fast_model, representative_tokens, representative_tokens),
                estimated_latency_ms=1000.0, estimated_quality=0.5, estimated_reliability=0.9,
            ),
            ModelCandidate(
                candidate_id='strong', provider='shade', model_id=strong_model,
                capabilities=(), context_window=0,
                estimated_cost_usd=estimate_cost_usd(strong_model, representative_tokens, representative_tokens),
                estimated_latency_ms=1000.0, estimated_quality=0.9, estimated_reliability=0.9,
            ),
        ]

        scarce = _budget_scarce(state_dir, budget)
        policy = PolicyName.ECONOMY if scarce else PolicyName.BALANCED

        # A 'complex' request sets a quality floor 'fast' can't clear (its
        # uncertainty-discounted quality is 0.25, 'strong' is 0.65 — see the
        # heuristic estimates above) so BALANCED actually honors it instead
        # of favoring 'fast' on cost alone as it otherwise would with only
        # two candidates and no calibration data. Dropped entirely once the
        # tree is scarce: ECONOMY is specifically the policy that overrides
        # the caller's own request to conserve what's left, and a hard floor
        # would make that impossible.
        quality_floor = 0.4 if (task_complexity == 'complex' and not scarce) else None

        task = TaskProfile(
            task_id='shade-model-choice',
            complexity=1.0 if task_complexity == 'complex' else (0.2 if task_complexity == 'simple' else 0.5),
            quality_floor=quality_floor,
        )
        decision = route_models(task, candidates, policy=policy)
        if not decision.feasible:
            return task_complexity
        return 'complex' if decision.selected_candidate_id == 'strong' else 'normal'
    except Exception as e:
        _diag('model_registry', 'shade model routing failed; falling back to plain task_complexity', error=e)
        return task_complexity
