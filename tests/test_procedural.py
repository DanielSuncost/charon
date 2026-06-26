"""Tests for the procedural memory layer (procedural.py)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'apps' / 'core-daemon'))

from memory_engine import MemoryEngine
import procedural as pr


def _engine():
    return MemoryEngine(Path(tempfile.mkdtemp()))


def test_learn_get_list_procedure():
    eng = _engine()
    p = pr.learn_procedure(eng, name="Rotate keys", goal="rotate the JWT signing keys",
                           steps=["read current key", "generate new key", "deploy", "revoke old"],
                           container_tag="t")
    assert p.id and p.steps[0] == "read current key"
    got = pr.get_procedure(eng, p.id)
    assert got is not None and got.goal == "rotate the JWT signing keys"
    assert [x.id for x in pr.list_procedures(eng, "t")] == [p.id]


def test_recall_procedures_by_goal():
    eng = _engine()
    pr.learn_procedure(eng, name="Add DB index", goal="speed up a slow database query",
                       steps=["EXPLAIN the query", "add an index", "re-run EXPLAIN"], container_tag="t")
    pr.learn_procedure(eng, name="Onboard user", goal="add a new teammate to the project",
                       steps=["create account", "grant roles"], container_tag="t")
    hits = pr.recall_procedures(eng, "my database query is too slow", container_tag="t", limit=3)
    assert hits and hits[0][0].name == "Add DB index"


def test_record_outcome_updates_success_rate():
    eng = _engine()
    p = pr.learn_procedure(eng, name="Deploy", goal="deploy the service", steps=["build", "ship"],
                           container_tag="t")
    base = pr.success_rate(p)
    pr.record_outcome(eng, p.id, success=True)
    pr.record_outcome(eng, p.id, success=True)
    up = pr.get_procedure(eng, p.id)
    assert up.success_count == 2 and up.failure_count == 0
    assert pr.success_rate(up) > base


def test_recall_reranks_by_success_rate():
    eng = _engine()
    good = pr.learn_procedure(eng, name="Deploy via blue-green", goal="deploy the service safely",
                              steps=["spin up green", "shift traffic", "retire blue"], container_tag="t")
    bad = pr.learn_procedure(eng, name="Deploy via big-bang push", goal="deploy the service safely",
                             steps=["push to prod directly"], container_tag="t")
    for _ in range(5):
        pr.record_outcome(eng, good.id, success=True)
    for _ in range(5):
        pr.record_outcome(eng, bad.id, success=False)
    hits = pr.recall_procedures(eng, "deploy the service safely", container_tag="t", limit=2)
    assert hits[0][0].id == good.id  # the one that keeps working ranks first


def test_distill_from_episodes_mines_recurring_pattern():
    eng = _engine()
    episodes = [
        {"id": "e1", "objective": "fix a failing flaky test", "success": True,
         "steps": ["Read", "Edit", "Bash"]},
        {"id": "e2", "objective": "fix a failing flaky test", "success": True,
         "steps": ["Read", "Edit", "Bash"]},
        {"id": "e3", "objective": "write release notes", "success": True,
         "steps": ["Read", "Write"]},                       # singleton -> not distilled
        {"id": "e4", "objective": "fix a failing flaky test", "success": False,
         "steps": ["Bash"]},                                 # failure -> ignored
    ]
    learned = pr.distill_from_episodes(eng, episodes, min_support=2, container_tag="t")
    assert len(learned) == 1
    proc = learned[0]
    assert proc.steps == ["Read", "Edit", "Bash"]
    assert set(proc.provenance) == {"e1", "e2"}
    # idempotent: re-running learns nothing new (goal already covered)
    assert pr.distill_from_episodes(eng, episodes, min_support=2, container_tag="t") == []
