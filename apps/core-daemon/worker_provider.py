from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_WORKER_MODELS = {
    'codex': 'gpt-5.4',
    'lmstudio': 'qwen3-30b-a3b',
}


def _read_json(path: Path, default=None):
    if not path.exists():
        return default or {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else (default or {})
    except Exception:
        return default or {}


def list_available_worker_providers(state_dir: Path) -> list[str]:
    """Return worker providers that appear selectable right now.

    Current policy:
    - codex if auth credentials exist in auth/auth.json
    - lmstudio as a supported local option
    """
    state_dir = Path(state_dir)
    available: list[str] = []
    auth = _read_json(state_dir / 'auth' / 'auth.json', {})
    providers = auth.get('providers') or {}

    codex = providers.get('openai-codex') or {}
    codex_tokens = codex.get('tokens') or {}
    if str(codex_tokens.get('access_token') or '').strip() or str(codex.get('api_key') or '').strip():
        available.append('codex')

    # Always offer LM Studio as a local worker option.
    # Reachability can be validated later; this keeps setup discoverable.
    available.append('lmstudio')

    # stable unique order
    out: list[str] = []
    for item in available:
        if item not in out:
            out.append(item)
    return out


def _model_matches_provider(model_id: str, provider_raw: str) -> bool:
    model_id = str(model_id or '').strip().lower()
    provider_raw = str(provider_raw or '').strip().lower()
    if not model_id:
        return True
    if provider_raw in ('lmstudio', 'local', 'ollama'):
        return model_id.startswith('lmstudio/') or '/' not in model_id
    if provider_raw in ('codex', 'openai', 'openai-codex', 'api', 'opencode'):
        return not model_id.startswith('lmstudio/')
    return True


def get_worker_provider_status(state_dir: Path) -> dict[str, Any]:
    """Inspect effective worker provider config and report whether it is usable."""
    state_dir = Path(state_dir)
    available = list_available_worker_providers(state_dir)

    try:
        from model_registry import load_registry
        reg = load_registry(state_dir)
    except Exception:
        reg = {}

    mode = str(reg.get('shade_model_mode') or 'auto').strip().lower()
    shade_provider = str(reg.get('shade_provider') or '').strip().lower()
    shade_model = str(reg.get('shade_model') or '').strip()
    shade_api_key = str(reg.get('shade_api_key') or '').strip()
    shade_base_url = str(reg.get('shade_base_url') or '').strip()

    if mode == 'fixed':
        if not shade_provider or not shade_model:
            return {
                'ok': False,
                'reason': 'no_provider',
                'available_providers': available,
                'mode': mode,
                'configured_provider': shade_provider,
                'configured_model': shade_model,
            }
        if not _model_matches_provider(shade_model, shade_provider):
            return {
                'ok': False,
                'reason': 'mismatch',
                'available_providers': available,
                'mode': mode,
                'configured_provider': shade_provider,
                'configured_model': shade_model,
            }
        if shade_provider in ('lmstudio', 'local', 'ollama'):
            return {
                'ok': True,
                'reason': '',
                'available_providers': available,
                'mode': mode,
                'configured_provider': shade_provider,
                'configured_model': shade_model,
                'base_url': shade_base_url or 'http://127.0.0.1:1234/v1',
            }
        if shade_provider in ('codex', 'openai', 'openai-codex'):
            auth = _read_json(state_dir / 'auth' / 'auth.json', {})
            providers = auth.get('providers') or {}
            codex = providers.get('openai-codex') or {}
            codex_tokens = codex.get('tokens') or {}
            if shade_api_key or str(codex_tokens.get('access_token') or '').strip() or str(codex.get('api_key') or '').strip():
                return {
                    'ok': True,
                    'reason': '',
                    'available_providers': available,
                    'mode': mode,
                    'configured_provider': shade_provider,
                    'configured_model': shade_model,
                }
            return {
                'ok': False,
                'reason': 'unready',
                'available_providers': available,
                'mode': mode,
                'configured_provider': shade_provider,
                'configured_model': shade_model,
            }
        return {
            'ok': False,
            'reason': 'unready',
            'available_providers': available,
            'mode': mode,
            'configured_provider': shade_provider,
            'configured_model': shade_model,
        }

    return {
        'ok': True,
        'reason': '',
        'available_providers': available,
        'mode': mode,
        'configured_provider': shade_provider,
        'configured_model': shade_model,
    }


def apply_worker_provider_choice(state_dir: Path, provider_choice: str) -> dict[str, Any]:
    state_dir = Path(state_dir)
    provider_choice = str(provider_choice or '').strip().lower()
    if provider_choice not in ('codex', 'lmstudio'):
        raise ValueError(f'unsupported worker provider choice: {provider_choice}')

    from model_registry import load_registry, save_registry

    reg = load_registry(state_dir)
    reg['shade_model_mode'] = 'fixed'
    reg['shade_provider'] = provider_choice
    reg['shade_model'] = DEFAULT_WORKER_MODELS[provider_choice]

    if provider_choice == 'lmstudio':
        reg['shade_base_url'] = 'http://127.0.0.1:1234/v1'
        reg['shade_api_key'] = 'not-needed'
    else:
        reg['shade_base_url'] = None
        reg['shade_api_key'] = None

    save_registry(state_dir, reg)
    return {
        'provider': provider_choice,
        'model': reg['shade_model'],
        'mode': reg['shade_model_mode'],
    }


def maybe_apply_answered_worker_provider_choice(state_dir: Path) -> dict[str, Any] | None:
    state_dir = Path(state_dir)
    clar_path = state_dir / 'clarifications.json'
    if not clar_path.exists():
        return None
    try:
        data = json.loads(clar_path.read_text())
    except Exception:
        return None
    items = data.get('items') or []
    changed = False
    for row in reversed(items):
        if row.get('status') != 'answered':
            continue
        question = str(row.get('question') or '').lower()
        if 'which provider should i use for worker tasks' not in question:
            continue
        if row.get('applied_at'):
            return None
        answer = str(row.get('answer') or '').strip().lower()
        if answer not in ('codex', 'lmstudio'):
            return None
        result = apply_worker_provider_choice(state_dir, answer)
        row['applied_at'] = _now_iso()
        row['applied_result'] = result
        changed = True
        if changed:
            clar_path.write_text(json.dumps(data, indent=2))
        return result
    return None


def ensure_worker_provider_or_request_clarification(state_dir: Path, *, ctx=None, purpose: str = 'worker tasks') -> dict[str, Any]:
    maybe_apply_answered_worker_provider_choice(state_dir)
    status = get_worker_provider_status(state_dir)
    if status.get('ok'):
        return status

    choices = list(status.get('available_providers') or [])
    question = (
        f'No usable provider is configured for {purpose}. '
        f'Available providers: {", ".join(choices) if choices else "(none detected)"}. '
        'Which provider should I use for worker tasks?'
    )
    clarification = None
    if ctx is not None:
        try:
            from tools.clarify_tool import execute_clarify
            pending_path = Path(state_dir) / 'clarifications.json'
            existing = None
            if pending_path.exists():
                try:
                    pending_data = json.loads(pending_path.read_text())
                    for row in reversed(pending_data.get('items') or []):
                        if row.get('status') == 'pending' and str(row.get('question') or '') == question:
                            existing = row
                            break
                except Exception:
                    existing = None
            if existing:
                clarification = existing
            else:
                result = execute_clarify({'action': 'ask', 'question': question, 'choices': choices[:4]}, ctx)
                clarification = result.details or {}
        except Exception:
            clarification = None
    status['clarification'] = clarification or {}
    status['question'] = question
    return status


def request_worker_provider_for_background_flow(
    state_dir: Path,
    *,
    purpose: str,
    agent_id: str = '',
    project_root: Path | None = None,
) -> dict[str, Any]:
    from tools import ToolContext

    ctx = ToolContext(
        project_root=Path(project_root) if project_root else Path.cwd(),
        agent_id=agent_id,
        state_dir=Path(state_dir),
    )
    return ensure_worker_provider_or_request_clarification(state_dir, ctx=ctx, purpose=purpose)
