"""Procedural memory layer over MemoryEngine — learned how-to, reinforced by outcomes.

Semantic memory stores *facts*; episodic memory stores *events*; procedural memory
stores *how to do things* — reusable step sequences distilled from past task
episodes, retrieved by goal, and reinforced by whether they actually worked.

A `Procedure` is a named, goal-scoped sequence of steps with success/failure
counts. It is:
  - learned     : explicitly (`learn_procedure`) or distilled from recurring
                  successful task episodes (`distill_from_episodes`);
  - retrieved   : `recall_procedures(goal)` matches the current goal via the same
                  vector+FTS recall (the procedure text is indexed as a memory),
                  then re-ranks by relevance × success rate;
  - reinforced  : `record_outcome(id, success)` updates the counts, so procedures
                  that keep working float up and ones that keep failing sink. This
                  ties procedural memory to verifiable outcomes (the judge/gate
                  loop), not just to having been written down.

Self-contained: owns one table, reuses the engine's add()/recall(). No change to
the memories schema.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from charon.memory.memory_engine import _now, _uuid


@dataclass
class Procedure:
    id: str
    container_tag: str = "default"
    name: str = ""
    goal: str = ""                       # the kind of task this applies to
    steps: list[str] = field(default_factory=list)
    preconditions: str = ""
    success_count: int = 0
    failure_count: int = 0
    last_used: str | None = None
    provenance: list[str] = field(default_factory=list)  # source task-episode ids
    summary_memory_id: str | None = None
    created_at: str = ""
    updated_at: str = ""


def success_rate(p: Procedure) -> float:
    """Laplace-smoothed success rate — an unproven procedure sits near 0.5, a
    repeatedly-successful one approaches 1, a repeatedly-failing one approaches 0."""
    return (p.success_count + 1) / (p.success_count + p.failure_count + 2)


def ensure_schema(db) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS procedures (
            id                TEXT PRIMARY KEY,
            container_tag     TEXT NOT NULL DEFAULT 'default',
            name              TEXT NOT NULL DEFAULT '',
            goal              TEXT NOT NULL DEFAULT '',
            steps_json        TEXT NOT NULL DEFAULT '[]',
            preconditions     TEXT NOT NULL DEFAULT '',
            success_count     INTEGER NOT NULL DEFAULT 0,
            failure_count     INTEGER NOT NULL DEFAULT 0,
            last_used         TEXT,
            provenance_json   TEXT NOT NULL DEFAULT '[]',
            summary_memory_id TEXT,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_procedures_container ON procedures(container_tag);
        """
    )
    db.commit()


def _row_to_proc(row) -> Procedure:
    return Procedure(
        id=row["id"], container_tag=row["container_tag"], name=row["name"], goal=row["goal"],
        steps=json.loads(row["steps_json"]), preconditions=row["preconditions"],
        success_count=row["success_count"], failure_count=row["failure_count"],
        last_used=row["last_used"], provenance=json.loads(row["provenance_json"]),
        summary_memory_id=row["summary_memory_id"], created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _proc_text(name: str, goal: str, steps: list[str]) -> str:
    return f"Procedure: {name}\nGoal: {goal}\nSteps: " + " ; ".join(steps)


def learn_procedure(engine, *, name: str, goal: str, steps: list[str],
                    container_tag: str = "default", preconditions: str = "",
                    provenance: list[str] | None = None) -> Procedure:
    """Store a reusable procedure and index it (goal+steps) for retrieval."""
    db = engine._get_db()
    ensure_schema(db)
    pid = _uuid()
    now = _now()
    provenance = list(provenance or [])
    mem = engine.add(_proc_text(name, goal, steps), category="procedure",
                     container_tag=container_tag, check_updates=False)
    db.execute(
        "INSERT INTO procedures (id, container_tag, name, goal, steps_json, preconditions, "
        "success_count, failure_count, last_used, provenance_json, summary_memory_id, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, container_tag, name, goal, json.dumps(steps), preconditions, 0, 0, None,
         json.dumps(provenance), mem.id, now, now),
    )
    db.commit()
    return Procedure(id=pid, container_tag=container_tag, name=name, goal=goal, steps=steps,
                     preconditions=preconditions, provenance=provenance,
                     summary_memory_id=mem.id, created_at=now, updated_at=now)


def get_procedure(engine, procedure_id: str) -> Procedure | None:
    db = engine._get_db()
    ensure_schema(db)
    row = db.execute("SELECT * FROM procedures WHERE id = ?", (procedure_id,)).fetchone()
    return _row_to_proc(row) if row else None


def list_procedures(engine, container_tag: str | None = None) -> list[Procedure]:
    db = engine._get_db()
    ensure_schema(db)
    if container_tag:
        rows = db.execute("SELECT * FROM procedures WHERE container_tag = ?", (container_tag,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM procedures").fetchall()
    return [_row_to_proc(r) for r in rows]


def record_outcome(engine, procedure_id: str, success: bool) -> Procedure | None:
    """Reinforce a procedure by the verifiable outcome of using it."""
    db = engine._get_db()
    ensure_schema(db)
    col = "success_count" if success else "failure_count"
    db.execute(
        f"UPDATE procedures SET {col} = {col} + 1, last_used = ?, updated_at = ? WHERE id = ?",
        (_now(), _now(), procedure_id),
    )
    db.commit()
    return get_procedure(engine, procedure_id)


def recall_procedures(engine, goal: str, *, container_tag: str | None = None,
                      limit: int = 5) -> list[tuple[Procedure, float]]:
    """Applicable procedures for `goal`, ranked by relevance × success rate."""
    db = engine._get_db()
    ensure_schema(db)
    res = engine.recall(goal, container_tag=container_tag, limit=limit * 5)
    seen, scored = set(), []
    for sm in res.memories:
        if sm.memory.category != "procedure":
            continue
        row = db.execute(
            "SELECT * FROM procedures WHERE summary_memory_id = ?", (sm.memory.id,)
        ).fetchone()
        if row and row["id"] not in seen:
            seen.add(row["id"])
            proc = _row_to_proc(row)
            scored.append((proc, sm.score * success_rate(proc)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


def _norm_goal(text: str) -> str:
    return " ".join(text.lower().split())[:80]


def distill_from_episodes(engine, episodes: list[dict], *, min_support: int = 2,
                          container_tag: str = "default") -> list[Procedure]:
    """Mine recurring successful task patterns into procedures.

    `episodes` is a list of dicts with at least: 'objective' (str), 'steps'
    (list[str] of tool/action names), and optionally 'success' (bool, default True)
    and 'id'. Episodes are grouped by normalized objective; any group with
    >= min_support successful episodes yields a procedure whose steps are the most
    common step sequence in that group. Only learns goals not already covered."""
    groups: dict[str, list[dict]] = {}
    for e in episodes:
        if not e.get("success", True):
            continue
        groups.setdefault(_norm_goal(e.get("objective", "")), []).append(e)

    existing = {_norm_goal(p.goal) for p in list_procedures(engine, container_tag)}
    learned = []
    for goal_key, members in groups.items():
        if len(members) < min_support or goal_key in existing or not goal_key:
            continue
        # modal step sequence (as a tuple) among the group's members
        seqs = [tuple(m.get("steps", [])) for m in members if m.get("steps")]
        if not seqs:
            continue
        modal = max(set(seqs), key=seqs.count)
        objective = members[0].get("objective", goal_key)
        name = objective[:60]
        learned.append(learn_procedure(
            engine, name=name, goal=objective, steps=list(modal),
            container_tag=container_tag,
            provenance=[m["id"] for m in members if m.get("id")],
        ))
    return learned


__all__ = [
    "Procedure", "ensure_schema", "learn_procedure", "get_procedure", "list_procedures",
    "record_outcome", "recall_procedures", "distill_from_episodes", "success_rate",
]
