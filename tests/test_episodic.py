"""Tests for the first-class episodic memory layer (episodic.py)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'apps' / 'core-daemon'))

import pytest
from memory_engine import MemoryEngine
import episodic as ep


def _engine():
    return MemoryEngine(Path(tempfile.mkdtemp()))


def _seed_two_sessions(eng):
    eng.add("Maria just started a new job at Northwind.", category="event",
            container_tag="t", source_conv="s0", source_turn=0, event_date="2025-01-01")
    eng.add("The weather was nice that afternoon.", category="event",
            container_tag="t", source_conv="s0", source_turn=1, event_date="2025-01-01")
    eng.add("Northwind is headquartered in Lyon.", category="event",
            container_tag="t", source_conv="s1", source_turn=0, event_date="2025-02-01")


def test_create_get_list_episode():
    eng = _engine()
    m = eng.add("session one content", category="event", container_tag="t",
                source_conv="c1", event_date="2025-03-01")
    e = ep.create_episode(eng, "a summary of session one", source_conv="c1",
                          member_ids=[m.id], container_tag="t")
    assert e.id and e.member_count == 1 and e.source_conv == "c1"
    got = ep.get_episode(eng, e.id)
    assert got is not None and got.summary == "a summary of session one"
    listed = ep.list_episodes(eng, "t")
    assert [x.id for x in listed] == [e.id]


def test_referenceable_memory_to_episode():
    eng = _engine()
    m1 = eng.add("fact one", category="event", container_tag="t", source_conv="c1")
    m2 = eng.add("fact two", category="event", container_tag="t", source_conv="c1")
    e = ep.create_episode(eng, "summary", source_conv="c1", member_ids=[m1.id, m2.id], container_tag="t")
    # memory -> episode
    assert ep.episode_for_memory(eng, m1.id).id == e.id
    assert ep.episode_for_memory(eng, m2.id).id == e.id
    # episode -> members
    assert set(ep.episode_members(eng, e.id)) == {m1.id, m2.id}


def test_segment_by_conversation_one_per_conv_and_dates():
    eng = _engine()
    _seed_two_sessions(eng)
    eps = ep.segment_by_conversation(eng, container_tag="t")
    by_conv = {e.source_conv: e for e in eps}
    assert set(by_conv) == {"s0", "s1"}
    assert by_conv["s0"].member_count == 2  # excludes nothing; both s0 turns
    assert by_conv["s0"].start_date == "2025-01-01" and by_conv["s0"].end_date == "2025-01-01"
    assert by_conv["s1"].start_date == "2025-02-01"


def test_segmentation_is_idempotent():
    eng = _engine()
    _seed_two_sessions(eng)
    first = ep.segment_by_conversation(eng, container_tag="t")
    second = ep.segment_by_conversation(eng, container_tag="t")  # no new convs
    assert len(first) == 2 and len(second) == 0
    assert len(ep.list_episodes(eng, "t")) == 2


def test_summary_is_indexed_and_recallable():
    eng = _engine()
    _seed_two_sessions(eng)
    ep.segment_by_conversation(eng, container_tag="t")
    # the episode summary surfaces as a first-class memory in ordinary recall
    r = eng.recall("Maria new job Northwind", container_tag="t", limit=6)
    assert any(sm.memory.category == "episode_summary" for sm in r.memories)
    # and recall_episodes resolves summaries back to Episode objects
    hits = ep.recall_episodes(eng, "Maria started a new job", container_tag="t", limit=5)
    assert hits and hits[0][0].source_conv == "s0"


def test_recency_weight_prefers_newer_among_matches():
    eng = _engine()
    # three distinct-but-related memories so dedup/version logic doesn't merge them
    eng.add("Deploy checklist for the alpha release rollout", category="event",
            container_tag="t", source_conv="c", event_date="2025-01-01", check_updates=False)
    eng.add("Deploy checklist for the beta release rollout", category="event",
            container_tag="t", source_conv="c", event_date="2025-03-01", check_updates=False)
    newest = eng.add("Deploy checklist for the gamma release rollout", category="event",
                     container_tag="t", source_conv="c", event_date="2025-09-01", check_updates=False)
    # With a strong recency bonus, the newest matching memory should rank first.
    r = eng.recall("deploy checklist release rollout", container_tag="t",
                   limit=5, recency_weight=1.0)
    assert r.memories[0].memory.id == newest.id
    assert r.memories[0].memory.event_date == "2025-09-01"


def test_recency_weight_zero_is_noop_default():
    eng = _engine()
    a = eng.add("alpha note about caching", category="event", container_tag="t",
                event_date="2025-01-01", check_updates=False)
    b = eng.add("beta note about caching", category="event", container_tag="t",
                event_date="2025-09-01", check_updates=False)
    # default (no recency_weight) must behave exactly like an explicit 0.0
    r_default = eng.recall("caching note", container_tag="t", limit=5)
    r_zero = eng.recall("caching note", container_tag="t", limit=5, recency_weight=0.0)
    assert [m.memory.id for m in r_default.memories] == [m.memory.id for m in r_zero.memories]
