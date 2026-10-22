"""A signed-in provider with no chosen model reports that, not an invented default."""
from __future__ import annotations

import json

from charon.providers.provider_bridge import missing_model_message, resolve_provider_config


def _state(tmp_path, **onboarding):
    (tmp_path / 'onboarding.json').write_text(json.dumps(onboarding))
    return tmp_path


def test_provider_without_model_is_explicit_not_gpt_4o(tmp_path):
    for complete in (True, False):
        cfg = resolve_provider_config(_state(tmp_path, provider_mode='provider', provider='codex', complete=complete))
        assert cfg['model_missing'] and cfg['model_id'] == '' and not cfg['ready']
        assert cfg['suggested_model'] == 'gpt-6-astra'          # catalog, not a frozen literal
        assert missing_model_message(cfg) == 'No model configured for codex. Pick one with /model (e.g. /model gpt-6-astra).'


def test_chosen_model_is_used_as_is(tmp_path):
    cfg = resolve_provider_config(_state(tmp_path, provider_mode='provider', provider='codex', complete=True, model='gpt-6-astra'))
    assert cfg['model_id'] == 'gpt-6-astra' and not cfg.get('model_missing')
