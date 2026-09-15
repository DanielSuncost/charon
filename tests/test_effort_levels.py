"""One effort ladder, per-model support, clamping — charon.providers.effort."""
import pytest

from charon.providers.effort import (
    CODEX_DEFAULT_EFFORTS,
    EFFORT_LADDER,
    THINKING_LEVELS,
    clamp_effort,
    normalize_thinking_level,
    step_down,
    supported_efforts,
)


def test_ladder_is_ordered_and_off_sits_outside_it():
    assert EFFORT_LADDER == ('minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
    assert THINKING_LEVELS[0] == 'off' and THINKING_LEVELS[1:] == EFFORT_LADDER


def test_normalize_keeps_max_and_ultra_as_their_own_levels():
    assert normalize_thinking_level('max') == 'max'          # used to alias to 'high'
    assert normalize_thinking_level(' ULTRA ') == 'ultra'
    assert normalize_thinking_level('min') == 'minimal'
    assert normalize_thinking_level('med') == 'medium'
    assert normalize_thinking_level('') == 'off'
    assert normalize_thinking_level(None) == 'off'
    assert normalize_thinking_level('banana') == 'off'
    assert normalize_thinking_level('banana', default='medium') == 'medium'


def test_astra_wire_ladder_is_low_to_max():
    # Live 400s pin both ends: 'minimal' and 'ultra' are rejected for astra.
    assert supported_efforts('gpt-6-astra') == ('low', 'medium', 'high', 'xhigh', 'max')


def test_unknown_models_get_the_conservative_floor_and_variants_inherit():
    assert supported_efforts('gpt-5') == CODEX_DEFAULT_EFFORTS == ('low', 'medium', 'high')
    assert supported_efforts('gpt-5.5-codex-preview') == supported_efforts('gpt-5.5')
    assert supported_efforts('') == CODEX_DEFAULT_EFFORTS


@pytest.mark.parametrize('level', ['low', 'medium', 'high', 'xhigh', 'max'])
def test_clamp_is_identity_on_astra_for_every_wire_level(level):
    assert clamp_effort(level, 'gpt-6-astra') == level


def test_clamp_folds_only_where_a_model_actually_stops():
    assert clamp_effort('ultra', 'gpt-6-astra') == 'max'     # CLI mode, not a wire value
    assert clamp_effort('minimal', 'gpt-6-astra') == 'low'   # rejected live; the floor instead
    assert clamp_effort('off', 'gpt-6-astra') == 'off'
    assert clamp_effort('xhigh', 'gpt-5') == 'high'          # unknown model: the old ladder
    assert clamp_effort('minimal', 'gpt-5') == 'low'          # …including the old minimal→low fold
    assert clamp_effort('ultra', 'gpt-5') == 'high'
    assert clamp_effort('ultra', 'gpt-5.5') == 'xhigh'
    assert clamp_effort('max', 'gpt-5.5') == 'xhigh'
    assert clamp_effort('ultra', 'gpt-5.6-luna') == 'max'
    assert clamp_effort('garbage', 'gpt-6-astra') == 'medium'  # the transport's old default


def test_step_down_moves_one_notch_and_never_below_the_floor():
    assert step_down('high') == 'medium'
    assert step_down('medium') == 'low'
    assert step_down('low') == 'low'
    assert step_down('ultra') == 'max'
    assert step_down('xhigh', floor='high') == 'high'
    assert step_down('minimal') == 'minimal'                  # never raised, either
    assert step_down('off') == 'off'
