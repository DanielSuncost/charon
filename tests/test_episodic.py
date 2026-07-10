"""Tests for the first-class episodic memory layer (episodic.py)."""
import tempfile
from pathlib import Path

import pytest
from charon.memory.memory_engine import MemoryEngine
from charon.memory import episodic as ep


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
    eng.add("alpha note about caching", category="event", container_tag="t",
            event_date="2025-01-01", check_updates=False)
    eng.add("beta note about caching", category="event", container_tag="t",
            event_date="2025-09-01", check_updates=False)
    # default (no recency_weight) must behave exactly like an explicit 0.0
    r_default = eng.recall("caching note", container_tag="t", limit=5)
    r_zero = eng.recall("caching note", container_tag="t", limit=5, recency_weight=0.0)
    assert [m.memory.id for m in r_default.memories] == [m.memory.id for m in r_zero.memories]


def _episode_on(eng, sid, date, body):
    m = eng.add(f"{body} session {sid}", category="event", container_tag="t",
                source_conv=sid, event_date=date, check_updates=False)
    return ep.create_episode(eng, f"summary {sid} {body}", source_conv=sid,
                             member_ids=[m.id], container_tag="t", title=sid)


def test_recent_episodes_orders_by_time():
    eng = _engine()
    _episode_on(eng, "a", "2025-01-01", "alpha")
    _episode_on(eng, "b", "2025-03-01", "beta")
    _episode_on(eng, "c", "2025-06-01", "gamma")
    recent = ep.recent_episodes(eng, "t", n=2)
    assert [e.source_conv for e in recent] == ["c", "b"]


def test_episodes_in_range_filters_by_window():
    eng = _engine()
    _episode_on(eng, "a", "2025-01-10", "alpha")
    _episode_on(eng, "b", "2025-02-15", "beta")
    _episode_on(eng, "c", "2025-03-20", "gamma")
    got = {e.source_conv for e in ep.episodes_in_range(eng, "2025-02-01", "2025-02-28", "t")}
    assert got == {"b"}


def test_episode_before_and_after():
    eng = _engine()
    ea = _episode_on(eng, "a", "2025-01-01", "alpha")
    eb = _episode_on(eng, "b", "2025-02-01", "beta")
    ec = _episode_on(eng, "c", "2025-03-01", "gamma")
    assert ep.episode_before(eng, eb.id, "t").source_conv == "a"
    assert ep.episode_after(eng, eb.id, "t").source_conv == "c"
    assert ep.episode_before(eng, ea.id, "t") is None   # nothing earlier
    assert ep.episode_after(eng, ec.id, "t") is None     # nothing later


# ── typed sub-events (finer granularity / MIRIX-style) ──────────────────────

def _ep(eng):
    m = eng.add("session body", category="event", container_tag="t", source_conv="s",
                event_date="2025-01-01", check_updates=False)
    return ep.create_episode(eng, "s summary", source_conv="s", member_ids=[m.id], container_tag="t")


def test_add_and_get_typed_events_in_order():
    eng = _engine()
    e = _ep(eng)
    ep.add_event(eng, e.id, event_type="user_message", actor="user",
                 summary="asked to add rate limiting", container_tag="t")
    ep.add_event(eng, e.id, event_type="tool_call", actor="tool",
                 summary="ran the test suite", refs={"tool": "Bash"}, container_tag="t")
    ep.add_event(eng, e.id, event_type="agent_message", actor="agent",
                 summary="implemented a token bucket", container_tag="t")
    evs = ep.get_events(eng, e.id)
    assert [x.event_type for x in evs] == ["user_message", "tool_call", "agent_message"]
    assert [x.seq for x in evs] == [0, 1, 2]               # ordered, monotonic
    assert evs[1].refs == {"tool": "Bash"}


def test_event_type_validation():
    eng = _engine()
    e = _ep(eng)
    with pytest.raises(ValueError):
        ep.add_event(eng, e.id, event_type="not_a_type", summary="x", container_tag="t")


def test_get_events_filtered_by_type_and_importance():
    eng = _engine()
    e = _ep(eng)
    ep.add_event(eng, e.id, event_type="user_message", summary="a question", importance=80, container_tag="t")
    ep.add_event(eng, e.id, event_type="observation", summary="noise", importance=10, container_tag="t")
    ep.add_event(eng, e.id, event_type="user_message", summary="another question", importance=70, container_tag="t")
    assert len(ep.get_events(eng, e.id, event_type="user_message")) == 2
    assert len(ep.get_events(eng, e.id, min_importance=50)) == 2   # drops the noise


def test_recall_events_retrieves_specific_moment_by_content_and_type():
    eng = _engine()
    e = _ep(eng)
    ep.add_event(eng, e.id, event_type="tool_result", actor="tool",
                 summary="the integration test failed with a timeout", container_tag="t")
    ep.add_event(eng, e.id, event_type="decision", actor="agent",
                 summary="decided to switch to an async client", container_tag="t")
    hits = ep.recall_events(eng, "which test failed", container_tag="t", limit=3)
    assert hits and hits[0][0].event_type == "tool_result"
    typed = ep.recall_events(eng, "what did we decide", container_tag="t", limit=3, event_type="decision")
    assert typed and all(ev.event_type == "decision" for ev, _ in typed)


def test_events_from_task_derivation():
    eng = _engine()
    e = _ep(eng)
    ev = ep.events_from_task(
        eng, e.id, objective="add rate limiting to the API",
        tool_calls=[{"tool": "Read"}, {"tool": "Bash", "arguments": {"command": "pytest"}}],
        response_text="Added a token-bucket limiter and tests.", container_tag="t",
        ts="2025-02-01",
    )
    types = [x.event_type for x in ev]
    assert types == ["user_message", "tool_call", "tool_call", "agent_message"]
    stored = ep.get_events(eng, e.id)
    assert [x.event_type for x in stored] == types          # all persisted, in order
    # only the message events are content-indexed (volume gating); tool calls aren't
    assert ev[0].summary_memory_id and ev[3].summary_memory_id
    assert ev[1].summary_memory_id is None and ev[2].summary_memory_id is None
    # the tool_call carries its tool ref
    assert ev[2].refs == {"tool": "Bash"}
