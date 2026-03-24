"""Tests for context assembler."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))
sys.path.insert(0, str(ROOT / 'libs'))

from context_store import ContextStore
from context_assembler import (
    ContextAssembler, AssembleResult,
    _format_summary_xml, _build_recall_guidance,
)
from providers import Message, ToolCall


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
        return dict(zip(cols, row))

    def fetchall(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@pytest.fixture
def db():
    d = FakeDB()
    ContextStore.ensure_schema(d)
    return d


# ── Tests ───────────────────────────────────────────────────────────

class TestSummaryXML:
    def test_leaf_summary_format(self, db):
        sid = ContextStore.insert_summary(
            db, agent_id='a', kind='leaf', depth=0,
            content='Summary text here.',
            earliest_at='2026-03-01T10:00:00',
            latest_at='2026-03-01T11:00:00')
        summary = ContextStore.get_summary(db, sid)
        xml = _format_summary_xml(summary)

        assert '<summary' in xml
        assert 'kind="leaf"' in xml
        assert 'depth="0"' in xml
        assert 'Summary text here.' in xml
        assert '</summary>' in xml

    def test_condensed_summary_includes_parents(self, db):
        s1 = ContextStore.insert_summary(
            db, agent_id='a', kind='leaf', depth=0, content='leaf 1')
        s2 = ContextStore.insert_summary(
            db, agent_id='a', kind='leaf', depth=0, content='leaf 2')
        cond = ContextStore.insert_summary(
            db, agent_id='a', kind='condensed', depth=1,
            content='Condensed.',
            parent_summary_ids=[s1, s2])

        summary = ContextStore.get_summary(db, cond)
        xml = _format_summary_xml(summary)

        assert '<parents>' in xml
        assert f'id="{s1}"' in xml
        assert f'id="{s2}"' in xml


class TestRecallGuidance:
    def test_no_guidance_without_summaries(self):
        result = _build_recall_guidance(0, 0, 0)
        assert result is None

    def test_light_guidance_shallow(self):
        result = _build_recall_guidance(2, 0, 0)
        assert result is not None
        assert 'precision questions' in result.lower() or 'search' in result.lower()
        assert 'deeply compacted' not in result.lower()

    def test_heavy_guidance_deep(self):
        result = _build_recall_guidance(5, 3, 3)
        assert result is not None
        assert 'deeply compacted' in result.lower()
        assert 'uncertainty checklist' in result.lower()


class TestAssembler:
    def test_empty_context(self, db):
        assembler = ContextAssembler(fresh_tail_count=4)
        result = assembler.assemble(db, 'agent-1', token_budget=100_000)
        assert result.messages == []
        assert result.estimated_tokens == 0

    def test_basic_assembly(self, db):
        for i in range(5):
            ContextStore.persist_message(
                db, 'agent-1',
                Message(role='user' if i % 2 == 0 else 'assistant',
                        content=f'Message {i}'))

        assembler = ContextAssembler(fresh_tail_count=10)
        result = assembler.assemble(db, 'agent-1', token_budget=100_000)

        assert len(result.messages) == 5
        assert result.estimated_tokens > 0
        assert result.system_prompt_addition is None  # no summaries

    def test_fresh_tail_always_included(self, db):
        """Even when over budget, fresh tail is never dropped."""
        for i in range(10):
            ContextStore.persist_message(
                db, 'agent-1',
                Message(role='user', content='x' * 400))

        assembler = ContextAssembler(fresh_tail_count=4)
        # Budget so small only fresh tail fits
        result = assembler.assemble(db, 'agent-1', token_budget=500)

        assert len(result.messages) >= 4

    def test_oldest_evicted_first(self, db):
        for i in range(10):
            ContextStore.persist_message(
                db, 'agent-1',
                Message(role='user', content=f'msg-{i} ' + 'x' * 200))

        assembler = ContextAssembler(fresh_tail_count=3)
        # Budget tight enough to evict some
        result = assembler.assemble(db, 'agent-1', token_budget=400)

        # Should have at least the 3 fresh tail messages
        assert len(result.messages) >= 3
        # Remaining messages should be the newest ones
        contents = [m.content if isinstance(m.content, str) else '' for m in result.messages]
        # The last 3 should always be present
        assert any('msg-9' in c for c in contents)
        assert any('msg-8' in c for c in contents)
        assert any('msg-7' in c for c in contents)

    def test_summaries_assembled_as_user_messages(self, db):
        # Add a summary to context
        sid = ContextStore.insert_summary(
            db, agent_id='agent-1', kind='leaf', depth=0,
            content='Summary of earlier work.')

        # Manually add it to context window
        db.execute(
            "INSERT INTO context_window "
            "(agent_id, ordinal, item_type, summary_id, token_count) "
            "VALUES (?, ?, 'summary', ?, ?)",
            ('agent-1', 0, sid, 50))
        db.commit()

        # Add a recent message
        ContextStore.persist_message(
            db, 'agent-1', Message(role='user', content='Continue'))

        assembler = ContextAssembler(fresh_tail_count=5)
        result = assembler.assemble(db, 'agent-1', token_budget=100_000)

        assert len(result.messages) == 2
        # Summary should be a user message with XML
        summary_msg = result.messages[0]
        assert summary_msg.role == 'user'
        content = summary_msg.content if isinstance(summary_msg.content, str) else ''
        assert '<summary' in content
        assert 'Summary of earlier work.' in content

    def test_recall_guidance_when_summaries_present(self, db):
        sid = ContextStore.insert_summary(
            db, agent_id='agent-1', kind='leaf', depth=0,
            content='A summary.')

        db.execute(
            "INSERT INTO context_window "
            "(agent_id, ordinal, item_type, summary_id, token_count) "
            "VALUES (?, ?, 'summary', ?, ?)",
            ('agent-1', 0, sid, 50))
        db.commit()

        ContextStore.persist_message(
            db, 'agent-1', Message(role='user', content='Hi'))

        assembler = ContextAssembler(fresh_tail_count=5)
        result = assembler.assemble(db, 'agent-1', token_budget=100_000)

        assert result.system_prompt_addition is not None
        assert 'Recall' in result.system_prompt_addition

    def test_tool_call_messages_reconstructed(self, db):
        tc = ToolCall(id='tc-1', name='Read', arguments={'path': 'f.py'})
        ContextStore.persist_message(
            db, 'agent-1',
            Message(role='assistant', content='Reading', tool_calls=[tc]))
        ContextStore.persist_message(
            db, 'agent-1',
            Message(role='tool_result', content='file data',
                    tool_call_id='tc-1', tool_name='Read'))

        assembler = ContextAssembler(fresh_tail_count=10)
        result = assembler.assemble(db, 'agent-1', token_budget=100_000)

        assert len(result.messages) == 2
        assert result.messages[0].role == 'assistant'
        assert len(result.messages[0].tool_calls) == 1
        assert result.messages[0].tool_calls[0].name == 'Read'
        assert result.messages[1].role == 'tool_result'
        assert result.messages[1].tool_call_id == 'tc-1'

    def test_stats_reported(self, db):
        ContextStore.persist_message(
            db, 'agent-1', Message(role='user', content='hello'))

        sid = ContextStore.insert_summary(
            db, agent_id='agent-1', kind='leaf', depth=0,
            content='summary')
        db.execute(
            "INSERT INTO context_window "
            "(agent_id, ordinal, item_type, summary_id, token_count) "
            "VALUES (?, ?, 'summary', ?, ?)",
            ('agent-1', 99, sid, 50))
        db.commit()

        assembler = ContextAssembler(fresh_tail_count=10)
        result = assembler.assemble(db, 'agent-1', token_budget=100_000)

        assert result.stats['raw'] == 1
        assert result.stats['summaries'] == 1
        assert result.stats['total'] == 2
