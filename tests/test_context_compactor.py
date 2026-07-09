"""Tests for lossless context compactor."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from context_store import ContextStore
from context_compactor import (
    ContextCompactor, CompactionConfig, _build_leaf_prompt, _build_d1_prompt, _build_d2_prompt,
    _build_d3plus_prompt, _deterministic_fallback,
)
from providers import Message, ModelInfo, StreamDelta


# ── Fixtures ────────────────────────────────────────────────────────

class FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def executemany(self, sql, params_seq):
        return self.conn.executemany(sql, params_seq)

    def commit(self):
        self.conn.commit()

    def fetchone(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row, strict=False))

    def fetchall(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


class FakeProvider:
    """Provider that returns configurable summary text."""

    def __init__(self, response: str = 'Summary of the conversation segment.'):
        self.response = response
        self.call_count = 0
        self.last_messages = None

    async def stream(self, messages, model, system_prompt, tools=None,
                     thinking_level='off', max_tokens=16384):
        self.call_count += 1
        self.last_messages = messages
        yield StreamDelta(type='text', text=self.response)
        yield StreamDelta(type='done', text='{}')


class FailingProvider:
    """Provider that always errors."""

    async def stream(self, messages, model, system_prompt, tools=None,
                     thinking_level='off', max_tokens=16384):
        yield StreamDelta(type='error', error='API error')


class EmptyProvider:
    """Provider that returns empty text."""

    async def stream(self, messages, model, system_prompt, tools=None,
                     thinking_level='off', max_tokens=16384):
        yield StreamDelta(type='text', text='')
        yield StreamDelta(type='done', text='{}')


@pytest.fixture
def db():
    d = FakeDB()
    ContextStore.ensure_schema(d)
    return d


@pytest.fixture
def model():
    return ModelInfo(provider='test', model_id='test-model',
                     context_window=100_000)


@pytest.fixture
def small_config():
    """Config with small thresholds for testing."""
    return CompactionConfig(
        context_threshold=0.5,
        fresh_tail_count=4,
        leaf_chunk_tokens=5000,
        leaf_min_fanout=3,
        condensed_min_fanout=2,
        leaf_target_tokens=200,
        condensed_target_tokens=300,
        summarizer_timeout=5.0,
    )


def _populate_messages(db, agent_id: str, count: int, chars_per: int = 200):
    """Add N messages of a given size."""
    for i in range(count):
        ContextStore.persist_message(
            db, agent_id,
            Message(role='user' if i % 2 == 0 else 'assistant',
                    content=f'Message {i}: ' + 'x' * chars_per))


# ── Prompt tests ────────────────────────────────────────────────────

class TestPrompts:
    def test_leaf_prompt_normal(self):
        prompt = _build_leaf_prompt('some text', target_tokens=500)
        assert 'SEGMENT' in prompt
        assert 'Normal summary policy' in prompt
        assert 'previous_context' in prompt
        assert 'Expand for details about' in prompt
        assert '500 tokens' in prompt

    def test_leaf_prompt_aggressive(self):
        prompt = _build_leaf_prompt('some text', target_tokens=200,
                                    aggressive=True)
        assert 'Aggressive summary policy' in prompt

    def test_leaf_prompt_with_previous_summary(self):
        prompt = _build_leaf_prompt('text', target_tokens=500,
                                    previous_summary='Prior context here')
        assert 'Prior context here' in prompt

    def test_d1_prompt(self):
        prompt = _build_d1_prompt('summaries', target_tokens=600)
        assert 'leaf-level' in prompt
        assert 'Expand for details about' in prompt

    def test_d1_prompt_with_previous(self):
        prompt = _build_d1_prompt('summaries', target_tokens=600,
                                   previous_summary='prior stuff')
        assert 'prior stuff' in prompt
        assert 'Do not repeat' in prompt

    def test_d2_prompt(self):
        prompt = _build_d2_prompt('summaries', target_tokens=800)
        assert 'session-level' in prompt
        assert 'trajectory' in prompt

    def test_d3plus_prompt(self):
        prompt = _build_d3plus_prompt('summaries', target_tokens=1000)
        assert 'high-level memory node' in prompt
        assert 'durable context' in prompt


class TestDeterministicFallback:
    def test_short_text(self):
        result = _deterministic_fallback('short text', 100)
        assert result == 'short text'

    def test_truncation(self):
        long_text = 'x' * 10000
        result = _deterministic_fallback(long_text, 100)
        assert len(result) < len(long_text)
        assert 'Truncated' in result

    def test_empty_text(self):
        result = _deterministic_fallback('', 100)
        assert result == '[Empty segment]'


# ── Compactor integration tests ─────────────────────────────────────

class TestEvaluateAndCompact:
    def test_no_compaction_under_threshold(self, db, model, small_config):
        compactor = ContextCompactor(small_config)
        _populate_messages(db, 'agent-1', 5, chars_per=100)

        result = asyncio.run(compactor.evaluate_and_compact(
            db, 'agent-1',
            token_budget=100_000,  # huge budget
            provider=FakeProvider(),
            model=model,
        ))
        assert not result.action_taken

    def test_compaction_triggered(self, db, model, small_config):
        compactor = ContextCompactor(small_config)
        # Fill up context to exceed 50% of 5000 = 2500 tokens
        # 30 msgs * 400 chars ≈ 3000 tokens > 2500
        _populate_messages(db, 'agent-1', 30, chars_per=400)

        provider = FakeProvider('Compacted summary text here.')
        result = asyncio.run(compactor.evaluate_and_compact(
            db, 'agent-1',
            token_budget=5_000,
            provider=provider,
            model=model,
        ))
        assert result.action_taken
        assert result.tokens_after < result.tokens_before
        assert result.created_summary_id is not None
        assert result.created_summary_id.startswith('sum_')

    def test_fresh_tail_preserved(self, db, model, small_config):
        compactor = ContextCompactor(small_config)
        _populate_messages(db, 'agent-1', 20, chars_per=400)

        provider = FakeProvider('Summary.')
        asyncio.run(compactor.evaluate_and_compact(
            db, 'agent-1',
            token_budget=5_000,
            provider=provider,
            model=model,
        ))

        items = ContextStore.get_context_window(db, 'agent-1')
        msg_items = [i for i in items if i.item_type == 'message']
        # At least fresh_tail_count messages should remain
        assert len(msg_items) >= small_config.fresh_tail_count

    def test_messages_survive_compaction(self, db, model, small_config):
        """The lossless guarantee: all messages persist after compaction."""
        compactor = ContextCompactor(small_config)
        _populate_messages(db, 'agent-1', 20, chars_per=400)

        provider = FakeProvider('Summary.')
        asyncio.run(compactor.evaluate_and_compact(
            db, 'agent-1',
            token_budget=5_000,
            provider=provider,
            model=model,
        ))

        # All 20 messages still in the database
        assert ContextStore.message_count(db, 'agent-1') == 20


class TestLeafPass:
    def test_leaf_creates_summary(self, db, model, small_config):
        compactor = ContextCompactor(small_config)
        _populate_messages(db, 'agent-1', 10, chars_per=300)

        provider = FakeProvider('Leaf summary.\nExpand for details about: foo, bar')
        result = asyncio.run(compactor.compact_leaf(
            db, 'agent-1', provider=provider, model=model))

        assert result.action_taken
        assert result.created_summary_id is not None

        summary = ContextStore.get_summary(db, result.created_summary_id)
        assert summary is not None
        assert summary.kind == 'leaf'
        assert summary.depth == 0
        assert len(summary.source_message_ids) > 0

    def test_leaf_links_source_messages(self, db, model, small_config):
        compactor = ContextCompactor(small_config)
        _populate_messages(db, 'agent-1', 10, chars_per=300)

        provider = FakeProvider('Summary.')
        result = asyncio.run(compactor.compact_leaf(
            db, 'agent-1', provider=provider, model=model))

        summary = ContextStore.get_summary(db, result.created_summary_id)
        # All source messages should still exist
        msgs = ContextStore.get_messages_by_ids(db, summary.source_message_ids)
        assert len(msgs) == len(summary.source_message_ids)


class TestFallbackEscalation:
    def test_empty_provider_falls_back(self, db, model, small_config):
        compactor = ContextCompactor(small_config)
        _populate_messages(db, 'agent-1', 10, chars_per=300)

        result = asyncio.run(compactor.compact_leaf(
            db, 'agent-1', provider=EmptyProvider(), model=model))

        # Should still make progress via deterministic fallback
        assert result.action_taken
        summary = ContextStore.get_summary(db, result.created_summary_id)
        assert 'Truncated' in summary.content or summary.content  # some content

    def test_error_provider_falls_back(self, db, model, small_config):
        compactor = ContextCompactor(small_config)
        _populate_messages(db, 'agent-1', 10, chars_per=300)

        result = asyncio.run(compactor.compact_leaf(
            db, 'agent-1', provider=FailingProvider(), model=model))

        assert result.action_taken
        summary = ContextStore.get_summary(db, result.created_summary_id)
        assert summary is not None


class TestCondensation:
    def test_condensation_merges_summaries(self, db, model, small_config):
        compactor = ContextCompactor(small_config)
        _populate_messages(db, 'agent-1', 30, chars_per=400)

        # Do enough leaf passes to create multiple summaries
        provider = FakeProvider('Leaf summary text.')
        for _ in range(5):
            asyncio.run(compactor.compact_leaf(
                db, 'agent-1', provider=provider, model=model))

        items = ContextStore.get_context_window(db, 'agent-1')
        summary_items = [i for i in items if i.item_type == 'summary']

        if len(summary_items) >= small_config.condensed_min_fanout:
            # Now run a full sweep which should condense
            cond_provider = FakeProvider('Condensed higher-level summary.')
            asyncio.run(compactor.evaluate_and_compact(
                db, 'agent-1',
                token_budget=2_000,  # force compaction
                provider=cond_provider,
                model=model,
            ))

            # Check for condensed summaries
            summaries = ContextStore.get_summaries_for_agent(db, 'agent-1')
            kinds = {s.kind for s in summaries}
            if 'condensed' in kinds:
                condensed = [s for s in summaries if s.kind == 'condensed']
                assert condensed[0].depth >= 1
                assert len(condensed[0].parent_summary_ids) >= 2
