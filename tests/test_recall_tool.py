"""Tests for Recall tool and memory indexer integration."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "core-daemon"))

from tools import ToolContext
from tools.recall_tool import execute_recall, _get_engine
from memory_indexer import index_conversation_sync


def _make_ctx(tmp_path, agent_id="test-agent"):
    ctx = ToolContext(project_root=tmp_path)
    ctx.state_dir = tmp_path
    ctx.agent_id = agent_id
    return ctx


@pytest.fixture
def ctx(tmp_path):
    return _make_ctx(tmp_path)


@pytest.fixture
def seeded_ctx(tmp_path):
    """Context with some memories already indexed."""
    ctx = _make_ctx(tmp_path)

    from memory_engine import MemoryEngine
    engine = MemoryEngine(tmp_path)
    engine.add("User graduated with a degree in Business Administration", is_static=True)
    engine.add("User's daily commute is 45 minutes each way")
    engine.add("User prefers TypeScript with strict mode enabled", is_static=True)
    engine.add("User is working on migrating the auth module to OAuth2")
    engine.add("User's 5K personal best was 27:12")
    engine.add("User's 5K personal best is now 25:50")  # knowledge update
    engine.close()

    return ctx


class TestRecallTool:
    def test_empty_query_returns_error(self, ctx):
        result = execute_recall({'query': ''}, ctx)
        assert result.is_error

    def test_no_state_dir_returns_error(self):
        ctx = ToolContext(project_root=Path('/tmp'))
        ctx.state_dir = None
        result = execute_recall({'query': 'test'}, ctx)
        assert result.is_error

    def test_recall_with_no_memories(self, ctx):
        result = execute_recall({'query': 'anything'}, ctx)
        assert 'No memories' in result.content

    def test_recall_finds_relevant_memory(self, seeded_ctx):
        result = execute_recall({'query': 'What degree did the user get?'}, seeded_ctx)
        assert not result.is_error
        assert 'Business Administration' in result.content

    def test_recall_with_profile(self, seeded_ctx):
        result = execute_recall({'query': 'who is the user', 'include_profile': True}, seeded_ctx)
        assert not result.is_error
        assert 'User Profile' in result.content or 'Stable Facts' in result.content

    def test_recall_version_chain(self, seeded_ctx):
        result = execute_recall({'query': '5K personal best time'}, seeded_ctx)
        assert not result.is_error
        assert '25:50' in result.content

    def test_recall_limit(self, seeded_ctx):
        result = execute_recall({'query': 'user', 'limit': 2}, seeded_ctx)
        assert not result.is_error
        # Should have at most 2 results
        lines = [l for l in result.content.split('\n') if l.startswith('**')]
        assert len(lines) <= 2


class TestMemoryIndexer:
    def test_index_conversation_sync(self, tmp_path):
        turns = [
            {'role': 'user', 'content': 'I just finished reading The Great Gatsby for my book club'},
            {'role': 'assistant', 'content': 'Great choice! The Great Gatsby is a classic American novel.'},
            {'role': 'user', 'content': 'Yes, our book club meets every Thursday evening at the library'},
        ]
        count = index_conversation_sync(tmp_path, turns, agent_id='test', conv_id='conv-1')
        assert count >= 2  # at least the user turns (>30 chars)

        # Verify we can recall the indexed content
        from memory_engine import MemoryEngine
        engine = MemoryEngine(tmp_path)
        result = engine.recall('book club meeting')
        assert len(result.memories) > 0
        engine.close()

    def test_index_skips_short_turns(self, tmp_path):
        turns = [
            {'role': 'user', 'content': 'Hi'},
            {'role': 'assistant', 'content': 'Hello!'},
        ]
        count = index_conversation_sync(tmp_path, turns, agent_id='test', conv_id='conv-1')
        assert count == 0

    def test_index_handles_empty_turns(self, tmp_path):
        count = index_conversation_sync(tmp_path, [], agent_id='test', conv_id='conv-1')
        assert count == 0

    def test_index_handles_missing_deps(self, tmp_path):
        # This should not raise even if called
        count = index_conversation_sync(tmp_path, [{'role': 'user', 'content': 'x' * 50}])
        # Should work if deps are installed, return 0 or 1
        assert isinstance(count, int)
