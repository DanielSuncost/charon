"""Tests for cross-agent decision & discussion threads (threads.py)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'apps' / 'core-daemon'))

from memory_engine import MemoryEngine
import episodic as ep
import threads as th


def _engine():
    return MemoryEngine(Path(tempfile.mkdtemp()))


def _episode(eng, *, agent, conv, date, tag):
    m = eng.add(f"session {conv}", category="event", container_tag=tag,
                source_conv=conv, event_date=date, check_updates=False)
    return ep.create_episode(eng, f"{conv} summary", source_conv=conv, source_agent=agent,
                             member_ids=[m.id], container_tag=tag, summary_memory_id=m.id)


def _scenario(eng, tag):
    """3 agents, 3 sessions, one topic (auth): raised, decided, implemented."""
    ea = _episode(eng, agent="planner", conv="sA", date="2025-03-03", tag=tag)
    ep.add_event(eng, ea.id, event_type="user_message", actor="user",
                 summary="should we use JWT or sessions for auth?", container_tag=tag, ts="2025-03-03")
    eb = _episode(eng, agent="architect", conv="sB", date="2025-03-07", tag=tag)
    th.log_decision(eng, eb.id, what="use JWT for auth",
                    why="stateless scales across the fleet", alternatives="server-side sessions",
                    topic="auth", container_tag=tag)
    ec = _episode(eng, agent="impl", conv="sC", date="2025-03-09", tag=tag)
    ep.add_event(eng, ec.id, event_type="agent_message", actor="agent",
                 summary="implemented JWT auth with refresh tokens", container_tag=tag, ts="2025-03-09")
    return ea, eb, ec


def test_log_decision_timestamps_to_episode_and_captures_why():
    eng = _engine(); tag = "p"
    e = _episode(eng, agent="architect", conv="sB", date="2025-03-07", tag=tag)
    ev = th.log_decision(eng, e.id, what="use JWT", why="stateless", alternatives="sessions",
                         topic="auth", container_tag=tag)
    assert ev.event_type == "decision" and ev.ts == "2025-03-07"   # episode's date, not now
    assert "because stateless" in ev.summary
    import json
    assert json.loads(ev.details)["alternatives"] == "sessions"


def test_thread_is_cross_agent_chronological_and_attributed():
    eng = _engine(); tag = "p"
    _scenario(eng, tag)
    items = th.thread(eng, "auth authentication JWT", container_tag=tag)
    # spans all three agents — not siloed
    assert {it.agent for it in items} == {"planner", "architect", "impl"}
    # chronological
    assert [it.ts for it in items] == sorted(it.ts for it in items)
    # the decision carries its WHY
    dec = [it for it in items if it.event_type == "decision"][0]
    assert dec.agent == "architect" and "stateless" in dec.why
    # and it sits between the planning and the implementation in time
    order = [it.event_type for it in items]
    assert order.index("user_message") < order.index("decision") < order.index("agent_message")


def test_why_returns_rationale_alternatives_and_owner():
    eng = _engine(); tag = "p"
    _scenario(eng, tag)
    w = th.why(eng, "auth JWT decision", container_tag=tag)
    assert w and w[0]["agent"] == "architect"
    assert "stateless" in w[0]["why"]
    assert w[0]["alternatives"] == "server-side sessions"


def test_get_or_create_episode_for_session_dedups():
    eng = _engine(); tag = "p"
    m = eng.add("x", category="event", container_tag=tag, source_conv="s1", check_updates=False)
    e1 = ep.get_or_create_episode_for_session(eng, source_conv="s1", container_tag=tag,
                                              source_agent="A", member_ids=[m.id])
    e2 = ep.get_or_create_episode_for_session(eng, source_conv="s1", container_tag=tag,
                                              source_agent="A")
    assert e1.id == e2.id
    assert len(ep.list_episodes(eng, tag)) == 1


def test_timeline_tool_thread_why_log_decision(tmp_path):
    from tools import ToolContext
    from tools.timeline_tool import execute_timeline
    state = tmp_path / "state"; state.mkdir()
    ctx = ToolContext(project_root=Path(tmp_path / "proj"), agent_id="architect", state_dir=state)
    r = execute_timeline({'action': 'log_decision', 'what': 'use JWT for auth',
                          'why': 'stateless scales across the fleet',
                          'alternatives': 'server-side sessions', 'query': 'auth'}, ctx)
    assert not r.is_error
    t = execute_timeline({'action': 'thread', 'query': 'auth JWT authentication'}, ctx)
    assert not t.is_error and 'JWT' in t.content and 'architect' in t.content
    w = execute_timeline({'action': 'why', 'query': 'auth'}, ctx)
    assert not w.is_error and 'stateless' in w.content
