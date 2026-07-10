"""Tests for lossless context store."""
from __future__ import annotations

import sqlite3

import pytest

# Ensure src is on the path

from charon.context.context_store import ContextStore, _estimate_tokens
from charon.providers import Message, ToolCall


# ── Fixtures ────────────────────────────────────────────────────────

class FakeDB:
    """Minimal DB wrapper matching store_adapter.DB interface."""

    def __init__(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        cursor = self.conn.execute(sql, params)
        _ = cursor.lastrowid  # ensure it's populated
        return cursor

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


@pytest.fixture
def db():
    d = FakeDB()
    ContextStore.ensure_schema(d)
    return d


# ── Tests ───────────────────────────────────────────────────────────

class TestEstimateTokens:
    def test_basic(self):
        assert _estimate_tokens('hello') >= 1
        assert _estimate_tokens('a' * 400) == 100

    def test_empty(self):
        assert _estimate_tokens('') == 1  # min 1


class TestPersistMessage:
    def test_persist_user_message(self, db):
        msg = Message(role='user', content='Hello world')
        msg_id = ContextStore.persist_message(db, 'agent-1', msg)
        assert msg_id > 0

        stored = ContextStore.get_message(db, msg_id)
        assert stored is not None
        assert stored.role == 'user'
        assert stored.content == 'Hello world'
        assert stored.agent_id == 'agent-1'
        assert stored.seq == 0

    def test_persist_assistant_with_tool_calls(self, db):
        tc = ToolCall(id='tc-1', name='Read', arguments={'path': 'foo.py'})
        msg = Message(role='assistant', content='Reading file',
                      tool_calls=[tc])
        msg_id = ContextStore.persist_message(db, 'agent-1', msg)
        stored = ContextStore.get_message(db, msg_id)

        assert stored.role == 'assistant'
        assert len(stored.tool_calls) == 1
        assert stored.tool_calls[0].name == 'Read'
        assert stored.tool_calls[0].arguments == {'path': 'foo.py'}

    def test_persist_tool_result(self, db):
        msg = Message(role='tool_result', content='file contents here',
                      tool_call_id='tc-1', tool_name='Read')
        msg_id = ContextStore.persist_message(db, 'agent-1', msg)
        stored = ContextStore.get_message(db, msg_id)

        assert stored.role == 'tool_result'
        assert stored.tool_call_id == 'tc-1'
        assert stored.tool_name == 'Read'

    def test_sequential_seq_numbers(self, db):
        for i in range(5):
            ContextStore.persist_message(
                db, 'agent-1', Message(role='user', content=f'msg {i}'))

        messages = ContextStore.get_messages_for_agent(db, 'agent-1')
        seqs = [m.seq for m in messages]
        assert seqs == [0, 1, 2, 3, 4]

    def test_messages_never_deleted(self, db):
        """Messages persist even after context window is cleared."""
        for i in range(3):
            ContextStore.persist_message(
                db, 'agent-1', Message(role='user', content=f'msg {i}'))

        ContextStore.clear_context_window(db, 'agent-1')

        assert ContextStore.message_count(db, 'agent-1') == 3
        assert len(ContextStore.get_context_window(db, 'agent-1')) == 0


class TestContextWindow:
    def test_messages_appear_in_window(self, db):
        ContextStore.persist_message(
            db, 'agent-1', Message(role='user', content='hello'))
        ContextStore.persist_message(
            db, 'agent-1', Message(role='assistant', content='hi'))

        items = ContextStore.get_context_window(db, 'agent-1')
        assert len(items) == 2
        assert items[0].item_type == 'message'
        assert items[0].ordinal == 0
        assert items[1].ordinal == 1

    def test_token_count(self, db):
        ContextStore.persist_message(
            db, 'agent-1', Message(role='user', content='a' * 400))
        total = ContextStore.get_context_token_count(db, 'agent-1')
        assert total >= 100

    def test_agents_isolated(self, db):
        ContextStore.persist_message(
            db, 'agent-1', Message(role='user', content='for agent 1'))
        ContextStore.persist_message(
            db, 'agent-2', Message(role='user', content='for agent 2'))

        items_1 = ContextStore.get_context_window(db, 'agent-1')
        items_2 = ContextStore.get_context_window(db, 'agent-2')
        assert len(items_1) == 1
        assert len(items_2) == 1


class TestSummaries:
    def test_insert_leaf_summary(self, db):
        sid = ContextStore.insert_summary(
            db,
            agent_id='agent-1',
            kind='leaf',
            depth=0,
            content='Summary of messages 1-5',
            source_message_ids=[1, 2, 3, 4, 5],
        )
        assert sid.startswith('sum_')

        summary = ContextStore.get_summary(db, sid)
        assert summary is not None
        assert summary.kind == 'leaf'
        assert summary.depth == 0
        assert summary.source_message_ids == [1, 2, 3, 4, 5]

    def test_insert_condensed_summary(self, db):
        s1 = ContextStore.insert_summary(
            db, agent_id='agent-1', kind='leaf', depth=0, content='leaf 1')
        s2 = ContextStore.insert_summary(
            db, agent_id='agent-1', kind='leaf', depth=0, content='leaf 2')

        cond = ContextStore.insert_summary(
            db,
            agent_id='agent-1',
            kind='condensed',
            depth=1,
            content='Condensed summary',
            parent_summary_ids=[s1, s2],
            descendant_count=2,
        )

        summary = ContextStore.get_summary(db, cond)
        assert summary.kind == 'condensed'
        assert summary.depth == 1
        assert summary.parent_summary_ids == [s1, s2]
        assert summary.descendant_count == 2


class TestReplaceRange:
    def test_replace_messages_with_summary(self, db):
        # Persist 10 messages
        for i in range(10):
            ContextStore.persist_message(
                db, 'agent-1', Message(role='user', content=f'msg {i}'))

        items_before = ContextStore.get_context_window(db, 'agent-1')
        assert len(items_before) == 10

        # Create a summary replacing messages 0-5
        sid = ContextStore.insert_summary(
            db, agent_id='agent-1', kind='leaf', depth=0,
            content='Summary of first 6 messages')

        ContextStore.replace_range_with_summary(
            db, 'agent-1',
            start_ordinal=0, end_ordinal=5,
            summary_id=sid, summary_token_count=50)

        items_after = ContextStore.get_context_window(db, 'agent-1')
        # 1 summary + 4 remaining messages = 5
        assert len(items_after) == 5
        assert items_after[0].item_type == 'summary'
        assert items_after[0].summary_id == sid
        assert items_after[1].item_type == 'message'

    def test_ordinals_stay_contiguous(self, db):
        for i in range(8):
            ContextStore.persist_message(
                db, 'agent-1', Message(role='user', content=f'msg {i}'))

        sid = ContextStore.insert_summary(
            db, agent_id='agent-1', kind='leaf', depth=0,
            content='Summary')

        ContextStore.replace_range_with_summary(
            db, 'agent-1', start_ordinal=0, end_ordinal=3,
            summary_id=sid, summary_token_count=50)

        items = ContextStore.get_context_window(db, 'agent-1')
        ordinals = [i.ordinal for i in items]
        # Should be contiguous starting from 0
        assert ordinals == list(range(len(items)))

    def test_raw_messages_survive_compaction(self, db):
        """The core lossless guarantee: messages are never deleted."""
        ids = []
        for i in range(10):
            mid = ContextStore.persist_message(
                db, 'agent-1', Message(role='user', content=f'msg {i}'))
            ids.append(mid)

        sid = ContextStore.insert_summary(
            db, agent_id='agent-1', kind='leaf', depth=0,
            content='Summary of all')

        ContextStore.replace_range_with_summary(
            db, 'agent-1', start_ordinal=0, end_ordinal=9,
            summary_id=sid, summary_token_count=50)

        # Context window has only the summary
        items = ContextStore.get_context_window(db, 'agent-1')
        assert len(items) == 1

        # But ALL messages are still in the database
        assert ContextStore.message_count(db, 'agent-1') == 10
        for mid in ids:
            msg = ContextStore.get_message(db, mid)
            assert msg is not None


class TestSearch:
    def test_search_messages(self, db):
        ContextStore.persist_message(
            db, 'agent-1', Message(role='user', content='Fix the auth bug'))
        ContextStore.persist_message(
            db, 'agent-1', Message(role='user', content='Deploy to staging'))

        results = ContextStore.search_messages(db, 'auth')
        assert len(results) == 1
        assert 'auth' in results[0].content

    def test_search_summaries(self, db):
        ContextStore.insert_summary(
            db, agent_id='agent-1', kind='leaf', depth=0,
            content='Fixed authentication module')
        ContextStore.insert_summary(
            db, agent_id='agent-1', kind='leaf', depth=0,
            content='Deployed database migration')

        results = ContextStore.search_summaries(db, 'authentication')
        assert len(results) == 1

    def test_search_across_compacted_history(self, db):
        """Search finds messages that have been compacted out of context."""
        ContextStore.persist_message(
            db, 'agent-1',
            Message(role='user', content='The error was ECONNREFUSED on port 5432'))

        # Compact it away
        sid = ContextStore.insert_summary(
            db, agent_id='agent-1', kind='leaf', depth=0,
            content='Database connection issue')
        ContextStore.replace_range_with_summary(
            db, 'agent-1', start_ordinal=0, end_ordinal=0,
            summary_id=sid, summary_token_count=50)

        # Not in context window
        items = ContextStore.get_context_window(db, 'agent-1')
        assert all(i.item_type == 'summary' for i in items)

        # But still searchable
        results = ContextStore.search_messages(db, 'ECONNREFUSED')
        assert len(results) == 1
        assert 'ECONNREFUSED' in results[0].content


class TestImport:
    def test_import_messages(self, db):
        messages = [
            Message(role='user', content='Hello'),
            Message(role='assistant', content='Hi there'),
        ]
        count = ContextStore.import_messages(db, 'agent-1', messages)
        assert count == 2
        assert ContextStore.message_count(db, 'agent-1') == 2

    def test_import_skips_if_exists(self, db):
        ContextStore.persist_message(
            db, 'agent-1', Message(role='user', content='existing'))

        count = ContextStore.import_messages(
            db, 'agent-1',
            [Message(role='user', content='new')])
        assert count == 0


class TestIntegrity:
    def test_clean_state(self, db):
        for i in range(5):
            ContextStore.persist_message(
                db, 'agent-1', Message(role='user', content=f'msg {i}'))
        issues = ContextStore.verify_integrity(db, 'agent-1')
        assert issues == []

    def test_detects_dangling_summary(self, db):
        ContextStore.persist_message(
            db, 'agent-1', Message(role='user', content='msg'))

        # Manually insert a bad context window entry
        db.execute(
            "INSERT INTO context_window "
            "(agent_id, ordinal, item_type, summary_id, token_count) "
            "VALUES (?, ?, 'summary', ?, ?)",
            ('agent-1', 99, 'sum_nonexistent', 50))
        db.commit()

        issues = ContextStore.verify_integrity(db, 'agent-1')
        assert any('Dangling summary' in i for i in issues)
