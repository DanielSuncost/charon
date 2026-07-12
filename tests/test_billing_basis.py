"""Cost figures must never present a per-token dollar estimate as a real cost
under an OAuth/subscription (flat-rate) or local (free) provider."""
import json
from pathlib import Path

from charon.providers import model_registry as mr


def test_provider_billing_mode_classification():
    assert mr.provider_billing_mode('codex') == 'subscription'
    assert mr.provider_billing_mode('claude-code') == 'subscription'
    assert mr.provider_billing_mode('anything', auth='oauth') == 'subscription'
    assert mr.provider_billing_mode('api') == 'metered'
    assert mr.provider_billing_mode('api', auth='api_key') == 'metered'
    assert mr.provider_billing_mode('lmstudio') == 'local'
    assert mr.provider_billing_mode('local') == 'local'
    assert mr.provider_billing_mode('') == 'metered'          # unknown -> safe default
    assert mr.cost_is_real('metered') is True
    assert mr.cost_is_real('subscription') is False
    assert mr.cost_is_real('local') is False


def test_resolve_billing_mode_from_onboarding(tmp_path, monkeypatch):
    # No registry shade provider -> fall back to onboarding provider/auth.
    monkeypatch.setattr(mr, 'load_registry', lambda *_a, **_k: {})
    (tmp_path / 'onboarding.json').write_text(json.dumps(
        {'provider': 'codex', 'provider_auth': 'oauth'}))
    assert mr.resolve_billing_mode(tmp_path) == 'subscription'

    (tmp_path / 'onboarding.json').write_text(json.dumps(
        {'provider': 'api', 'provider_auth': 'api_key'}))
    assert mr.resolve_billing_mode(tmp_path) == 'metered'


def test_resolve_billing_mode_prefers_shade_provider(tmp_path, monkeypatch):
    # A fixed codex shade provider is subscription even if onboarding says api.
    monkeypatch.setattr(mr, 'load_registry', lambda *_a, **_k: {'shade_provider': 'codex'})
    (tmp_path / 'onboarding.json').write_text(json.dumps(
        {'provider': 'api', 'provider_auth': 'api_key'}))
    assert mr.resolve_billing_mode(tmp_path) == 'subscription'


def test_resolve_billing_mode_defaults_metered(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, 'load_registry', lambda *_a, **_k: {})
    assert mr.resolve_billing_mode(tmp_path) == 'metered'  # nothing resolvable


def test_research_tool_cost_line_honesty():
    from charon.tools.research_tool import _cost_line
    assert 'subscription' in _cost_line({'cost_basis': 'subscription', 'estimated_cost_usd': 2.28})
    assert '$' not in _cost_line({'cost_basis': 'subscription', 'estimated_cost_usd': 2.28})
    assert 'local' in _cost_line({'cost_basis': 'local'})
    assert '$1.5000' in _cost_line({'cost_basis': 'metered', 'estimated_cost_usd': 1.5})


def test_report_render_suppresses_dollar_for_subscription():
    from charon.libris import libris_report as R
    data = {
        'topics': [{'title': 'T', 'report_md': '# T\n\nbody', 'claims': [], 'sources': [],
                    'checkpoints': [], 'slug': 't', 'why': '', 'status': '', 'focus_questions': []}],
        'claims': [], 'sources': [], 'sources_by_id': {},
        'usage': {'total_tokens': 1_000_000, 'estimated_cost_usd': 2.28, 'cost_basis': 'subscription'},
        'prompt': '', 'summary': '', 'status': '', 'operation_id': 'op', 'created_at': '',
    }
    html = R.render_html(data, title='T')
    assert '$2.28' not in html and 'subscription' in html
    assert '1,000,000' in html  # real token count still shown

    data['usage'] = {'total_tokens': 1_000_000, 'estimated_cost_usd': 2.28, 'cost_basis': 'metered'}
    html2 = R.render_html(data, title='T')
    assert '$2.28' in html2  # real metered cost IS shown
