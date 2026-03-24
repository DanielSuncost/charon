"""Tests for soft_specialization module."""
import json
import sys
import time
from pathlib import Path

import pytest

# Ensure core-daemon is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'apps' / 'core-daemon'))

from soft_specialization import (
    derive_label_heuristic,
    _score_topics,
    _tokenize,
    refresh_specialization,
    should_refresh,
    _last_refresh,
    _MIN_TASKS,
    REFRESH_INTERVAL_SEC,
)


class TestTokenize:
    def test_basic(self):
        tokens = _tokenize("edited auth/login.py, ran pytest")
        assert 'auth' in tokens
        assert 'login' in tokens
        assert 'pytest' in tokens
        # Stop words removed
        assert 'edited' not in tokens
        assert 'ran' not in tokens

    def test_underscores(self):
        tokens = _tokenize("store_adapter agent_runtime")
        assert 'store_adapter' in tokens
        assert 'agent_runtime' in tokens


class TestScoreTopics:
    def test_auth_keywords(self):
        summaries = [
            "Fixed token refresh in auth module",
            "Updated login flow credentials",
            "Added jwt validation to auth handler",
        ]
        scores = _score_topics(summaries)
        topics = [t for t, _ in scores]
        assert topics[0] == 'auth'

    def test_testing_keywords(self):
        summaries = [
            "Ran pytest, 5 tests passed",
            "Wrote test for auth module",
            "Fixed unittest assertion in test_login",
        ]
        scores = _score_topics(summaries)
        topics = [t for t, _ in scores]
        assert 'testing' in topics[:2]

    def test_shade_keywords(self):
        summaries = [
            "Implemented shade orchestrator phase advance",
            "Fixed contract phase spawning",
            "Added shade ephemeral cleanup",
        ]
        scores = _score_topics(summaries)
        topics = [t for t, _ in scores]
        assert topics[0] == 'shade'

    def test_empty_summaries(self):
        scores = _score_topics([])
        assert scores == []


class TestDeriveLabelHeuristic:
    def test_below_min_tasks(self):
        label = derive_label_heuristic(["one task"])
        assert label == ''

    def test_clear_auth_focus(self):
        summaries = [
            "Fixed token refresh in auth module",
            "Updated login flow credentials",
            "Added jwt validation",
            "Tested auth endpoint",
            "Refactored auth middleware",
        ]
        label = derive_label_heuristic(summaries)
        assert 'auth' in label

    def test_mixed_focus_combines(self):
        summaries = [
            "Updated TUI layout widget",
            "Fixed textual terminal rendering",
            "Edited ui_layout.py styling",
            "Added auth token to TUI panel",
            "Fixed auth display in widget",
        ]
        label = derive_label_heuristic(summaries)
        # Should detect both TUI and auth
        assert 'TUI' in label or 'auth' in label

    def test_database_focus(self):
        summaries = [
            "Added sqlite migration for new table",
            "Fixed store_adapter schema upgrade",
            "Updated database indexes",
            "Ran sql migration test",
        ]
        label = derive_label_heuristic(summaries)
        assert 'database' in label

    def test_no_signal(self):
        summaries = [
            "Did something",
            "Did another thing",
            "More stuff done",
        ]
        label = derive_label_heuristic(summaries)
        assert label == ''

    def test_window_only_recent(self):
        # Old tasks about auth, recent tasks about testing
        old = ["Fixed auth login"] * 15
        recent = [
            "Ran pytest suite",
            "Added test coverage",
            "Fixed test assertion",
            "Wrote new test file",
            "Test infrastructure update",
        ]
        # Window is 10 by default, so only last 10 matter
        all_summaries = old + recent
        label = derive_label_heuristic(all_summaries)
        assert 'testing' in label


class TestRefreshSpecialization:
    def test_should_refresh_initially(self):
        _last_refresh.clear()
        assert should_refresh('AG-TEST') is True

    def test_should_not_refresh_too_soon(self):
        _last_refresh['AG-TEST'] = time.time()
        assert should_refresh('AG-TEST') is False

    def test_should_refresh_after_interval(self):
        _last_refresh['AG-TEST'] = time.time() - REFRESH_INTERVAL_SEC - 1
        assert should_refresh('AG-TEST') is True

    def test_refresh_with_no_memory(self, tmp_path):
        """Should return None when agent has no working memory."""
        _last_refresh.clear()
        state_dir = tmp_path / '.charon_state'
        state_dir.mkdir()
        result = refresh_specialization(state_dir, 'AG-NONE', mode='heuristic')
        assert result is None

    def test_refresh_with_enough_memory(self, tmp_path):
        """Should derive a label when agent has enough task history."""
        _last_refresh.clear()
        state_dir = tmp_path / '.charon_state'
        agent_dir = state_dir / 'agents' / 'AG-TEST'
        agent_dir.mkdir(parents=True)

        # Write working memory with auth-focused summaries
        memory = {
            'agent_id': 'AG-TEST',
            'notes': [
                {'ts': '2026-03-23T00:00:00', 'task_id': f't{i}', 'summary': s}
                for i, s in enumerate([
                    "Fixed token refresh in auth module",
                    "Updated login flow credentials",
                    "Added jwt validation",
                    "Tested auth endpoint",
                    "Refactored auth middleware",
                ])
            ],
        }
        (agent_dir / 'working_memory.json').write_text(json.dumps(memory))

        result = refresh_specialization(state_dir, 'AG-TEST', mode='heuristic')
        assert result is not None
        assert 'auth' in result


class TestIntegrationWithAgentsJson:
    def test_writes_to_agents_json(self, tmp_path):
        """Specialization should be written back to agents.json."""
        _last_refresh.clear()
        state_dir = tmp_path / '.charon_state'
        agent_dir = state_dir / 'agents' / 'AG-WRITE'
        agent_dir.mkdir(parents=True)

        # Create agents.json
        agents = [{'id': 'AG-WRITE', 'name': 'test', 'status': 'running'}]
        (state_dir / 'agents.json').write_text(json.dumps(agents))

        # Write working memory
        memory = {
            'agent_id': 'AG-WRITE',
            'notes': [
                {'ts': '2026-03-23T00:00:00', 'task_id': f't{i}', 'summary': s}
                for i, s in enumerate([
                    "Updated sqlite store adapter",
                    "Added database migration for boundaries",
                    "Fixed sql schema version check",
                    "Ran database integration tests",
                ])
            ],
        }
        (agent_dir / 'working_memory.json').write_text(json.dumps(memory))

        refresh_specialization(state_dir, 'AG-WRITE', mode='heuristic')

        # Check agents.json was updated
        updated = json.loads((state_dir / 'agents.json').read_text())
        assert updated[0].get('specialization') == 'database'
