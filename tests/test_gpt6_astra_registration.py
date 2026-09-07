"""GPT-6 Astra (Sep 2026) must be registered everywhere a model's context
window or price is looked up, or Charon silently mis-sizes it.

CONTEXT_WINDOWS is an exact-match dict with a 65536 default, so a missing
entry is not a warning — it truncates a 1.05M-token model to 6% of its
window. These assertions exist to make the next model addition fail loudly
rather than quietly.
"""
from charon.context.context_transfer import MODEL_CONTEXT_OVERRIDES
from charon.infra import orchestration_trace as ot
from charon.providers.provider_bridge import CONTEXT_WINDOWS, DEFAULT_CONTEXT_WINDOW

ASTRA = 'gpt-6-astra'
ASTRA_CONTEXT = 1_050_000


def test_astra_context_window_is_registered_not_defaulted():
    assert CONTEXT_WINDOWS.get(ASTRA) == ASTRA_CONTEXT
    assert CONTEXT_WINDOWS[ASTRA] != DEFAULT_CONTEXT_WINDOW
    assert MODEL_CONTEXT_OVERRIDES.get(ASTRA) == ASTRA_CONTEXT


def test_astra_priced_at_standard_rate_and_not_via_gpt5_prefix():
    # $10 in / $50 out per 1M, short-context tier.
    assert ot.estimate_cost_usd(ASTRA, 1_000_000, 0) == 10.0
    assert ot.estimate_cost_usd(ASTRA, 0, 1_000_000) == 50.0
    # Must not fall through to the gpt-5 family rate or the 'fast' fallback.
    assert ot.estimate_cost_usd(ASTRA, 1_000_000, 0) != 1.25
    assert ot.estimate_cost_usd(ASTRA, 1_000_000, 0) != 0.15
