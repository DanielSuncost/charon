#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


def default_onboarding() -> dict:
    return {
        'complete': False,
        'step': 'provider-mode',
        'provider_mode': '',
        'provider': '',
        'provider_model': '',
        'provider_base_url': '',
        'model': '',
        'provider_auth': '',
        'opencode_provider': '',
        'opencode_model': '',
        'api_key': '',
        'project': '',
        'updated_at': '',
        'shade_provider': '',
        'shade_model': '',
        'shade_base_url': '',
    }


def load_onboarding(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return default_onboarding()
    base = default_onboarding()
    base.update({k: data.get(k, base[k]) for k in base.keys()})
    return base


def save_onboarding(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def onboarding_panel_text(state: dict) -> str:
    done = 'yes' if state.get('complete') else 'no'
    provider_mode = state.get('provider_mode') or '(unset)'
    provider = state.get('provider') or '(unset)'
    model = state.get('model') or '(unset)'
    project = state.get('project') or '(unset)'
    step = state.get('step') or 'model'
    shade_provider = state.get('shade_provider') or ''
    shade_model = state.get('shade_model') or ''
    shade_line = ''
    if shade_provider or shade_model:
        shade_line = f'Shade / parser model: {shade_provider or "(unset)"} / {shade_model or "(unset)"}\n'
    return (
        '[b]Setup / Onboarding[/b]\n'
        f'Complete: {done}   Current step: {step}\n'
        f'Mode: {provider_mode}\n'
        f'Provider: {provider}\n'
        f'Model: {model}\n'
        f'{shade_line}'
        f'Project: {project}\n'
        'Commands: /setup provider <name> | /setup model <name> | /setup shade-provider <name> | /setup shade-model <name> | /setup project <name> | /setup complete | /setup status | /setup reset\n'
        'The shade model is also used for lightweight orchestration/NL command parsing.'
    )
