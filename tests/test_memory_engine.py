"""Tests for the semantic memory engine."""
from __future__ import annotations


import pytest

from charon.memory.memory_engine import MemoryEngine


@pytest.fixture
def engine(tmp_path):
    e = MemoryEngine(tmp_path)
    yield e
    e.close()


# ── Basic CRUD ──────────────────────────────────────────────────────

class TestBasicOperations:
    def test_add_memory(self, engine):
        mem = engine.add("User prefers TypeScript with strict mode", category="preference")
        assert mem.id
        assert mem.content == "User prefers TypeScript with strict mode"
        assert mem.category == "preference"
        assert mem.is_latest is True
        assert mem.is_forgotten is False

    def test_add_empty_raises(self, engine):
        with pytest.raises(ValueError):
            engine.add("")

    def test_count(self, engine):
        assert engine.count() == 0
        engine.add("Fact one")
        assert engine.count() == 1
        engine.add("Fact two")
        assert engine.count() == 2

    def test_get(self, engine):
        mem = engine.add("User's name is Alice")
        fetched = engine.get(mem.id)
        assert fetched is not None
        assert fetched.content == "User's name is Alice"

    def test_get_nonexistent(self, engine):
        assert engine.get("nonexistent") is None

    def test_forget(self, engine):
        mem = engine.add("Temporary fact")
        assert engine.count() == 1
        engine.forget(mem.id)
        # Count excludes forgotten
        fetched = engine.get(mem.id)
        assert fetched.is_forgotten is True

    def test_container_tag_isolation(self, engine):
        engine.add("Fact for project A", container_tag="proj_a")
        engine.add("Fact for project B", container_tag="proj_b")
        assert engine.count("proj_a") == 1
        assert engine.count("proj_b") == 1
        assert engine.count() == 2


# ── Deduplication ───────────────────────────────────────────────────

class TestDeduplication:
    def test_near_duplicate_rejected(self, engine):
        engine.add("User prefers dark mode in all editors")
        engine.add("User prefers dark mode in all editors")  # exact dup
        assert engine.count() == 1

    def test_different_content_accepted(self, engine):
        engine.add("User prefers dark mode")
        engine.add("User's favorite language is Rust")
        assert engine.count() == 2


# ── Knowledge Updates (version chains) ──────────────────────────────

class TestKnowledgeUpdates:
    def test_update_creates_version_chain(self, engine):
        mem1 = engine.add("User's 5K personal best is 27:12", category="event")
        mem2 = engine.add("User's 5K personal best is 25:50", category="event")
        assert engine.count() == 2

        # The new one should be latest
        fetched2 = engine.get(mem2.id)
        assert fetched2.is_latest is True

        # The old one should not be latest
        fetched1 = engine.get(mem1.id)
        assert fetched1.is_latest is False

        # Version chain should link them
        assert fetched2.parent_id == mem1.id
        assert fetched2.version == 2

    def test_update_edge_created(self, engine):
        # Use very similar phrasing to trigger version detection
        engine.add("User's personal best 5K time is 27 minutes and 12 seconds")
        mem2 = engine.add("User's personal best 5K time is 25 minutes and 50 seconds")
        fetched2 = engine.get(mem2.id)
        has_chain = fetched2.parent_id is not None
        edges = engine.get_edges(mem2.id, "updates")
        assert has_chain or len(edges) >= 1


# ── Recall (hybrid search) ─────────────────────────────────────────

class TestRecall:
    def test_vector_recall(self, engine):
        engine.add("User graduated with a degree in Business Administration")
        engine.add("User enjoys hiking on weekends")
        engine.add("User has a 45 minute commute to work")

        result = engine.recall("What degree did the user graduate with?")
        assert len(result.memories) > 0
        # The business administration fact should be top-ranked
        top = result.memories[0].memory.content
        assert "business" in top.lower() or "administration" in top.lower()

    def test_fts_recall(self, engine):
        engine.add("User's favorite programming language is TypeScript")
        engine.add("User prefers dark mode in VS Code")

        result = engine.recall("TypeScript")
        assert len(result.memories) > 0
        assert any("typescript" in m.memory.content.lower() for m in result.memories)

    def test_recall_empty(self, engine):
        result = engine.recall("anything at all")
        assert len(result.memories) == 0
        assert result.confidence == 0.0

    def test_recall_with_profile(self, engine):
        engine.add("User's name is Alice", is_static=True)
        engine.add("User is working on auth migration", is_static=False)

        result = engine.recall("who is the user", include_profile=True)
        assert len(result.profile_static) > 0
        assert len(result.profile_dynamic) > 0

    def test_recall_container_filtering(self, engine):
        engine.add("Fact for project A", container_tag="proj_a")
        engine.add("Fact for project B", container_tag="proj_b")

        result_a = engine.recall("Fact", container_tag="proj_a")
        result_b = engine.recall("Fact", container_tag="proj_b")

        # Each should only find its own container's facts
        for m in result_a.memories:
            assert m.memory.container_tag == "proj_a"
        for m in result_b.memories:
            assert m.memory.container_tag == "proj_b"

    def test_recall_timing(self, engine):
        for i in range(50):
            engine.add(f"User fact number {i} about topic {i % 5}")
        result = engine.recall("topic 3")
        assert result.timing_ms < 5000  # should be well under 5s

    def test_recall_version_chain_in_results(self, engine):
        engine.add("User's favorite color is blue")
        engine.add("User's favorite color is now green")
        result = engine.recall("favorite color")
        assert len(result.memories) > 0
        # Should find the latest version with a chain
        latest = [m for m in result.memories if m.memory.is_latest]
        assert len(latest) > 0


# ── Temporal features ───────────────────────────────────────────────

class TestTemporal:
    def test_event_date_stored(self, engine):
        mem = engine.add("Visited MoMA", event_date="2023-05-15")
        fetched = engine.get(mem.id)
        assert fetched.event_date == "2023-05-15"

    def test_forget_after_expiry(self, engine):
        engine.add("Temporary note", forget_after="2020-01-01T00:00:00")
        assert engine.count() == 1
        expired = engine.expire_memories()
        assert expired >= 1

    def test_temporal_range_filter(self, engine):
        engine.add("Event in January", event_date="2023-01-15")
        engine.add("Event in June", event_date="2023-06-15")
        engine.add("Event in December", event_date="2023-12-15")

        result = engine.recall(
            "events",
            temporal_range=("2023-05-01", "2023-07-01"),
        )
        # Should preferentially find the June event
        if result.memories:
            dates = [m.memory.event_date for m in result.memories if m.memory.event_date]
            assert any("2023-06" in d for d in dates)


# ── Profile ─────────────────────────────────────────────────────────

class TestProfile:
    def test_profile_separates_static_dynamic(self, engine):
        engine.add("User's name is Alice", is_static=True)
        engine.add("User is working on Project X", is_static=False)
        result = engine.profile()
        assert "Alice" in " ".join(result.profile_static)
        assert "Project X" in " ".join(result.profile_dynamic)

    def test_profile_with_query(self, engine):
        engine.add("User prefers TypeScript", is_static=True)
        engine.add("User is debugging auth module", is_static=False)
        result = engine.profile(query="programming language")
        assert len(result.memories) > 0


# ── Edge management ─────────────────────────────────────────────────

class TestEdges:
    def test_add_and_get_edge(self, engine):
        mem1 = engine.add("User likes React")
        mem2 = engine.add("User completed React hooks tutorial")
        engine.add_edge(mem2.id, mem1.id, "extends", confidence=0.9)

        edges = engine.get_edges(mem1.id)
        assert len(edges) > 0
        assert edges[0]["edge_type"] == "extends"

    def test_get_edges_by_type(self, engine):
        mem1 = engine.add("Fact A")
        mem2 = engine.add("Fact B related to A but different enough to not be a dup",
                          check_updates=False)
        engine.add_edge(mem1.id, mem2.id, "extends")
        engine.add_edge(mem1.id, mem2.id, "derives")

        extends = engine.get_edges(mem1.id, "extends")
        derives = engine.get_edges(mem1.id, "derives")
        assert len(extends) == 1
        assert len(derives) == 1


# ── Batch operations ────────────────────────────────────────────────

class TestBatch:
    def test_add_batch(self, engine):
        facts = [
            {"content": "User prefers dark mode"},
            {"content": "User uses Vim keybindings"},
            {"content": "User's timezone is UTC-8"},
        ]
        mems = engine.add_batch(facts, container_tag="test")
        assert len(mems) == 3
        assert engine.count("test") == 3

    def test_add_batch_skips_empty(self, engine):
        facts = [
            {"content": "Valid fact"},
            {"content": ""},
            {"content": "Another valid fact"},
        ]
        mems = engine.add_batch(facts)
        assert len(mems) == 2
