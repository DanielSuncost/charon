"""Integration: completing a task (create_task_episode) creates a first-class,
queryable Episode in the live memory engine — i.e. episodic memory is wired in,
not just a library."""
from pathlib import Path

from charon.memory.execution_memory import create_task_episode
from charon.memory.memory_engine import MemoryEngine
from charon.memory import episodic as ep


def test_task_completion_creates_queryable_episode(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    proj = str(tmp_path / "proj")
    rec = create_task_episode(
        state, session_id="sess-1", agent_id="AG-1", project_root=proj, provider="codex",
        objective="add rate limiting to the public API",
        summary="Implemented a token-bucket limiter and added tests.",
        tool_calls=[{"tool": "Read"}, {"tool": "Edit"}, {"tool": "Bash"}],
        response_text="done", total_turns=4, input_tokens=100, output_tokens=50,
    )
    assert rec["tool_sequence"] == ["Read", "Edit", "Bash"]

    eng = MemoryEngine(state)
    tag = f"project:{Path(proj).resolve()}"
    eps = ep.list_episodes(eng, tag)
    assert len(eps) == 1 and eps[0].source_conv == "sess-1"

    # Phase B: the completed task populated typed sub-events on the episode
    events = ep.get_events(eng, eps[0].id)
    types = [e.event_type for e in events]
    assert "user_message" in types and "agent_message" in types
    assert types.count("tool_call") == 3                    # Read, Edit, Bash
    # and a specific moment is retrievable by content + type
    hits = ep.recall_events(eng, "rate limiting", container_tag=tag, limit=5,
                            event_type="user_message")
    assert hits and "rate limiting" in hits[0][0].summary
    # retrievable as an event by content (resolves the bridged task_episode handle)
    hits = ep.recall_episodes(eng, "rate limiting API", container_tag=tag, limit=3)
    assert hits and hits[0][0].source_conv == "sess-1"
    # and time-structurally
    assert ep.recent_episodes(eng, tag, n=1)[0].source_conv == "sess-1"


def test_two_tasks_are_ordered_episodes(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    proj = str(tmp_path / "proj")
    for i, obj in enumerate(["set up the database schema", "wire up the API layer"]):
        create_task_episode(
            state, session_id=f"sess-{i}", agent_id="AG", project_root=proj, provider="codex",
            objective=obj, summary=f"did {obj}", tool_calls=[{"tool": "Write"}],
            response_text="ok", total_turns=2, input_tokens=10, output_tokens=5,
        )
    eng = MemoryEngine(state)
    tag = f"project:{Path(proj).resolve()}"
    assert len(ep.list_episodes(eng, tag)) == 2
    recent = ep.recent_episodes(eng, tag, n=2)
    assert {e.source_conv for e in recent} == {"sess-0", "sess-1"}


def test_timeline_tool_registered_and_queries_episodes(tmp_path):
    from charon.tools import ToolContext, ALL_TOOL_DEFS, TOOL_EXECUTORS
    from charon.tools.timeline_tool import execute_timeline

    # registered alongside the other tools
    assert 'Timeline' in TOOL_EXECUTORS
    assert any(d.get('name') == 'Timeline' for d in ALL_TOOL_DEFS)

    state = tmp_path / "state"
    state.mkdir()
    proj = str(tmp_path / "proj")
    create_task_episode(
        state, session_id="s1", agent_id="AG", project_root=proj, provider="codex",
        objective="add caching to the API", summary="added a Redis cache layer",
        tool_calls=[{"tool": "Edit"}], response_text="ok", total_turns=2,
        input_tokens=1, output_tokens=1,
    )
    ctx = ToolContext(project_root=Path(proj), agent_id="AG", state_dir=state)
    recent = execute_timeline({'action': 'recent', 'n': 5}, ctx)
    assert not recent.is_error and 'caching' in recent.content
    topic = execute_timeline({'action': 'topic', 'query': 'caching'}, ctx)
    assert not topic.is_error and 'caching' in topic.content
