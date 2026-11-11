"""Context budget arithmetic and provider overflow detection.

Contract: docs/contracts/context-budget-and-recovery.md
"""
from __future__ import annotations

import pytest

from charon.context.context_budget import (
    MIN_HISTORY_BUDGET,
    MIN_PLAUSIBLE_WINDOW,
    SAFETY_MARGIN_TOKENS,
    clip_oversized_results,
    clip_text,
    learned_context_window,
    plan_budget,
    record_context_window,
    reply_reserve,
    reset_learned_context_windows,
    shrink_window,
)
from charon.providers import Message
from charon.providers.context_overflow import detect_context_overflow
from charon.providers.http_errors import http_error


@pytest.fixture(autouse=True)
def _clean_learned_windows():
    reset_learned_context_windows()
    yield
    reset_learned_context_windows()


# ── Reply reserve ───────────────────────────────────────────────────

@pytest.mark.parametrize('window, expected', [
    (2048, 1024),      # floor
    (8192, 2048),
    (16384, 4096),
    (32768, 8192),
    (65536, 16384),
    (200000, 32768),   # the configured cap
])
def test_reply_reserve_scales_with_the_window(window, expected):
    assert reply_reserve(window, max_tokens=32768) == expected


def test_reply_reserve_never_exceeds_a_small_output_cap():
    assert reply_reserve(200000, max_tokens=512) == 512


# ── Budget ──────────────────────────────────────────────────────────

def test_history_is_what_the_prompt_and_reply_leave():
    budget = plan_budget(
        8192, system_prompt='s' * 4000, tools=None, max_tokens=32768)

    assert budget.overhead == 1000
    assert budget.reply_reserve == 2048
    assert budget.history == 8192 - 1000 - 2048 - SAFETY_MARGIN_TOKENS


def test_tool_schemas_count_as_overhead():
    tools = [{'name': 'Read', 'description': 'd' * 400, 'input_schema': {}}]
    bare = plan_budget(8192, system_prompt='', tools=None, max_tokens=4096)
    with_tools = plan_budget(8192, system_prompt='', tools=tools, max_tokens=4096)

    assert with_tools.overhead > 100
    assert with_tools.history == bare.history - with_tools.overhead


def test_history_has_a_floor_when_the_prompt_fills_the_window():
    budget = plan_budget(
        4096, system_prompt='s' * 40000, tools=None, max_tokens=32768)

    assert budget.history == MIN_HISTORY_BUDGET


def test_large_window_keeps_its_configured_compaction_trigger():
    budget = plan_budget(
        200000, system_prompt='s' * 40000, tools=None, max_tokens=32768)

    assert budget.compaction_trigger(0.7) == 140000
    assert budget.compactor_budget(0.7) == 200000


def test_small_window_compacts_at_its_history_budget():
    budget = plan_budget(
        8192, system_prompt='s' * 12000, tools=None, max_tokens=32768)

    assert budget.history < int(8192 * 0.7)
    assert budget.compaction_trigger(0.7) == budget.history
    # A compactor that triggers at threshold * budget triggers at history.
    assert int(0.7 * budget.compactor_budget(0.7)) <= budget.history


def test_request_output_cap_is_the_configured_cap_while_it_fits():
    budget = plan_budget(200000, system_prompt='', tools=None, max_tokens=32768)

    assert budget.request_max_tokens(10000, 32768) == 32768


def test_request_output_cap_shrinks_to_the_room_left():
    budget = plan_budget(8192, system_prompt='s' * 4000, tools=None, max_tokens=32768)

    # 8192 - 1000 overhead - 3000 history - margin
    assert budget.request_max_tokens(3000, 32768) == 8192 - 1000 - 3000 - SAFETY_MARGIN_TOKENS


def test_request_output_cap_never_drops_below_the_reply_reserve():
    budget = plan_budget(8192, system_prompt='s' * 4000, tools=None, max_tokens=32768)

    assert budget.request_max_tokens(50000, 32768) == budget.reply_reserve


# ── Clipping ────────────────────────────────────────────────────────

def test_clip_text_keeps_head_and_tail_and_counts_the_gap():
    text = 'HEAD' + 'm' * 10000 + 'TAIL'
    clipped = clip_text(text, 1000)

    assert clipped.startswith('HEAD')
    assert clipped.endswith('TAIL')
    assert len(clipped) <= 1000
    kept = len(clipped) - len(clipped[clipped.index('\n[...'):clipped.index('...]\n') + 5])
    assert f'{len(text) - kept} characters omitted' in clipped


def test_clip_text_leaves_short_text_alone():
    assert clip_text('short', 1000) == 'short'


def test_only_oversized_tool_results_are_clipped_and_originals_are_untouched():
    budget = plan_budget(8192, system_prompt='', tools=None, max_tokens=32768)
    big = 'x' * (budget.result_cap * 4 + 4000)
    messages = [
        Message(role='user', content='y' * len(big)),
        Message(role='tool_result', content='small', tool_call_id='a'),
        Message(role='tool_result', content=big, tool_call_id='b'),
    ]

    shown = clip_oversized_results(messages, budget)

    assert shown[0] is messages[0], 'user content is never clipped'
    assert shown[1] is messages[1]
    assert shown[2] is not messages[2]
    assert shown[2].tool_call_id == 'b'
    assert 'characters omitted' in shown[2].content
    assert len(shown[2].content) < len(big)
    assert messages[2].content == big


def test_nothing_to_clip_returns_the_same_list():
    budget = plan_budget(200000, system_prompt='', tools=None, max_tokens=32768)
    messages = [Message(role='tool_result', content='ok', tool_call_id='a')]

    assert clip_oversized_results(messages, budget) is messages


# ── Window learning ─────────────────────────────────────────────────

def test_a_smaller_reported_window_is_adopted():
    assert shrink_window(65536, 8192) == 8192


def test_an_unhelpful_report_cuts_the_planning_window_by_a_fifth():
    # Same window as planned: the estimate was short, not the window wrong.
    assert shrink_window(8192, 8192) == int(8192 * 0.8)
    assert shrink_window(8192, None) == int(8192 * 0.8)
    assert shrink_window(8192, 131072) == int(8192 * 0.8)


def test_an_implausibly_small_report_is_ignored():
    assert shrink_window(8192, 12) == int(8192 * 0.8)


def test_the_planning_window_has_a_floor():
    assert shrink_window(MIN_PLAUSIBLE_WINDOW, None) == MIN_PLAUSIBLE_WINDOW


def test_learned_windows_only_move_down():
    assert learned_context_window('local', 'm') is None
    assert record_context_window('local', 'm', 8192) == 8192
    assert record_context_window('local', 'm', 16384) == 8192
    assert record_context_window('local', 'm', 4096) == 4096
    assert learned_context_window('local', 'm') == 4096
    assert learned_context_window('local', 'other') is None


# ── Overflow detection ──────────────────────────────────────────────

@pytest.mark.parametrize('text, status, window, prompt', [
    ("This model's maximum context length is 8192 tokens. However, you requested "
     "40960 tokens (8192 in the messages, 32768 in the completion). Please reduce "
     "the length of the messages or completion.", 400, 8192, 8192),
    ("This endpoint's maximum context length is 131072 tokens. However, you "
     "requested about 140000 tokens (139000 of text input, 1000 in the output).",
     400, 131072, 140000),
    ('the request exceeds the available context size, try increasing it '
     '(n_ctx=8192, n_prompt_tokens=9000)', 400, 8192, 9000),
    ('prompt is too long: 210000 tokens > 200000 maximum', 400, 200000, 210000),
    ('The input token count (1200000) exceeds the maximum number of tokens '
     'allowed (1048576).', 400, 1048576, 1200000),
    ('the model is loaded with context length of only 8,192 tokens', 500, 8192, None),
    ('context_length_exceeded', None, None, None),
])
def test_overflow_wordings_are_recognised(text, status, window, prompt):
    overflow = detect_context_overflow(text, status)

    assert overflow is not None
    assert overflow.window == window
    assert overflow.prompt_tokens == prompt


@pytest.mark.parametrize('text, status', [
    ('Rate limit reached on tokens per min (TPM): Limit 30000, Requested 40000', 429),
    ('Rate limit reached: context window quota, too many tokens per min', None),
    ('max_tokens: 40000 > 32000, which is the maximum allowed number of output tokens', 400),
    ('Invalid API key', 401),
    ('model not found; context length unknown', 404),
    ('Internal server error', 500),
    ('', 400),
    (None, 400),
])
def test_other_errors_are_not_overflows(text, status):
    assert detect_context_overflow(text, status) is None


def test_http_error_marks_an_overflow_and_does_not_call_it_retryable():
    delta = http_error('prompt is too long: 210000 tokens > 200000 maximum', status_code=400)

    assert delta.error_code == 'context_overflow'
    assert delta.retryable is False


def test_http_error_keeps_ordinary_errors_as_they_were():
    delta = http_error('upstream overloaded', status_code=503)

    assert delta.error_code == 'http_error'
    assert delta.retryable is True
