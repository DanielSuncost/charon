"""Decision auto-extraction: conservative heuristic capture of committed
decisions from agent output, and its wiring into task-completion episodes.
(Accuracy is measured separately: scripts/experiments/exp_decision_extraction.py.)"""
from pathlib import Path

from charon.agents.decision_extract import extract_decisions


def test_extracts_decision_with_rationale():
    out = extract_decisions("We decided to use Postgres for the ledger because we need real transactions.")
    assert len(out) == 1
    assert "Postgres" in out[0]["what"]
    assert "real transactions" in out[0]["why"]
    assert out[0]["importance"] == 80


def test_rationale_clause_stripped_from_what():
    out = extract_decisions("I chose SQLite for the cache layer, as it removes a service dependency.")
    assert out and "SQLite" in out[0]["what"]
    assert "service dependency" not in out[0]["what"]
    assert "service dependency" in out[0]["why"]


def test_rejects_questions_hedges_and_negations():
    for text in [
        "Should we use Redis or Memcached for this?",
        "We could go with GraphQL, but REST also works.",
        "We haven't decided on the hosting region.",
        "We're not going to use Kubernetes for this.",   # polarity guard: never
        "Maybe we adopt gRPC later.",                    # invert a negated commitment
    ]:
        assert extract_decisions(text) == [], text


def test_rejects_third_party_decisions():
    assert extract_decisions("The vendor decided to deprecate the v1 API.") == []
    assert extract_decisions("He decided to take vacation in August.") == []


def test_softer_commitment_gets_lower_importance():
    out = extract_decisions("Let's go with feature flags for the rollout.")
    assert out and out[0]["importance"] == 70


def test_caps_and_dedupes():
    text = ". ".join(f"We decided to use tool{i} because reason {i}" for i in range(6)) + "."
    assert len(extract_decisions(text)) == 3
    dup = "We decided to use ruff. We decided to use ruff."
    assert len(extract_decisions(dup)) == 1


def test_task_completion_auto_captures_decision(tmp_path):
    """Integration: a completed task whose response states a decision produces a
    queryable decision event with rationale — without any log_decision call."""
    from charon.memory.execution_memory import create_task_episode
    from charon.memory.memory_engine import MemoryEngine
    from charon.agents import threads as th
    import json as _json
    from charon.memory import episodic as ep

    state = tmp_path / "state"
    state.mkdir()
    proj = str(tmp_path / "proj")
    create_task_episode(
        state, session_id="s-dec", agent_id="AG-7", project_root=proj, provider="codex",
        objective="pick a linter for the repo",
        summary="Evaluated ruff and flake8.",
        tool_calls=[{"tool": "Bash"}],
        response_text="Compared both on the codebase. We decided to use ruff because it is fast.",
        total_turns=2, input_tokens=10, output_tokens=10,
    )
    eng = MemoryEngine(state)
    tag = f"project:{Path(proj).resolve()}"
    eps = ep.list_episodes(eng, tag)
    assert len(eps) == 1
    decisions = ep.get_events(eng, eps[0].id, event_type="decision")
    assert len(decisions) == 1
    assert "ruff" in decisions[0].summary
    assert _json.loads(decisions[0].details)["auto"] is True
    # and it surfaces through the cross-agent why() path
    w = th.why(eng, "which linter did we pick", container_tag=tag)
    assert w and "fast" in w[0]["why"]
