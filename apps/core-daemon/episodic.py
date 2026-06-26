"""First-class episodic memory layer over MemoryEngine.

An Episode groups a coherent set of memories (typically one conversation/session)
under a stable id, with a *summary that is itself indexed for retrieval*. That is
the key idea: a query can match a session's gist via its summary even when no
individual turn matches — which is exactly where flat turn-level retrieval breaks
(multi-session joins, temporal/abstractive queries).

Episodes are:
  - first-class:   their own `episodes` table, created/queried/listed directly;
  - referenceable: stable ids; `episode_for_memory()` resolves a memory -> its
                   episode, and the summary links back via `summary_memory_id`;
  - retrievable:   `recall_episodes()` returns episodes ranked by the same
                   vector+FTS+RRF machinery, and (because summaries are stored as
                   real memories) they also surface in ordinary `engine.recall()`.

This module is additive and self-contained: it owns two tables and reuses the
engine's `add()`/`recall()`. It does not modify the memories schema.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from memory_engine import _now, _uuid  # reuse engine helpers


@dataclass
class Episode:
    id: str
    container_tag: str = "default"
    source_conv: str | None = None
    title: str = ""
    summary: str = ""
    summary_memory_id: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    tags: str = ""
    member_count: int = 0
    created_at: str = ""
    updated_at: str = ""


# Typed event kinds, aligned with the standard episodic taxonomy (and MIRIX's
# user_message / inferred_result / system_notification event types).
EVENT_TYPES = (
    "user_message", "agent_message", "tool_call", "tool_result",
    "decision", "observation", "system_notification",
)


@dataclass
class EpisodeEvent:
    id: str
    episode_id: str
    container_tag: str = "default"
    ts: str = ""
    seq: int = 0
    event_type: str = "observation"
    actor: str = ""              # user / agent / tool / system
    summary: str = ""
    details: str = ""
    refs: dict = field(default_factory=dict)
    importance: int = 50
    summary_memory_id: str | None = None
    created_at: str = ""


def ensure_schema(db) -> None:
    """Create the episode tables if absent (lazy migration, idempotent)."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS episodes (
            id                TEXT PRIMARY KEY,
            container_tag     TEXT NOT NULL DEFAULT 'default',
            source_conv       TEXT,
            title             TEXT NOT NULL DEFAULT '',
            summary           TEXT NOT NULL DEFAULT '',
            summary_memory_id TEXT,
            start_date        TEXT,
            end_date          TEXT,
            tags              TEXT NOT NULL DEFAULT '',
            member_count      INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_episodes_container ON episodes(container_tag);
        CREATE INDEX IF NOT EXISTS idx_episodes_conv ON episodes(source_conv);
        CREATE INDEX IF NOT EXISTS idx_episodes_dates ON episodes(start_date, end_date);
        CREATE TABLE IF NOT EXISTS episode_members (
            episode_id  TEXT NOT NULL,
            memory_id   TEXT NOT NULL,
            PRIMARY KEY (episode_id, memory_id)
        );
        CREATE INDEX IF NOT EXISTS idx_epmem_mem ON episode_members(memory_id);

        -- Typed sub-events within an episode (the finer-granularity / MIRIX-style
        -- layer): discrete user/agent/tool/system events, each retrievable.
        CREATE TABLE IF NOT EXISTS episode_events (
            id                TEXT PRIMARY KEY,
            episode_id        TEXT NOT NULL,
            container_tag     TEXT NOT NULL DEFAULT 'default',
            ts                TEXT NOT NULL,
            seq               INTEGER NOT NULL DEFAULT 0,
            event_type        TEXT NOT NULL,
            actor             TEXT NOT NULL DEFAULT '',
            summary           TEXT NOT NULL DEFAULT '',
            details           TEXT NOT NULL DEFAULT '',
            refs_json         TEXT NOT NULL DEFAULT '{}',
            importance        INTEGER NOT NULL DEFAULT 50,
            summary_memory_id TEXT,
            created_at        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_epev_episode ON episode_events(episode_id, seq);
        CREATE INDEX IF NOT EXISTS idx_epev_type ON episode_events(event_type);
        """
    )
    db.commit()


def _row_to_episode(row) -> Episode:
    return Episode(
        id=row["id"], container_tag=row["container_tag"], source_conv=row["source_conv"],
        title=row["title"], summary=row["summary"], summary_memory_id=row["summary_memory_id"],
        start_date=row["start_date"], end_date=row["end_date"], tags=row["tags"],
        member_count=row["member_count"], created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _date_bounds(db, member_ids):
    if not member_ids:
        return None, None
    qs = ",".join("?" * len(member_ids))
    row = db.execute(
        f"SELECT MIN(event_date), MAX(event_date) FROM memories "
        f"WHERE id IN ({qs}) AND event_date IS NOT NULL",
        member_ids,
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def default_summarizer(contents: list[str]) -> str:
    """Deterministic fallback summary (join of member contents, truncated).

    Production callers pass an LLM summarizer; this keeps the layer usable and
    testable with no provider."""
    return " ".join(c.strip() for c in contents if c.strip())[:1500]


def create_episode(engine, summary: str, *, source_conv: str | None = None,
                   member_ids: list[str] | None = None, title: str = "", tags: str = "",
                   container_tag: str = "default", index_summary: bool = True,
                   summary_memory_id: str | None = None) -> Episode:
    """Create an episode, link member memories, and index its summary for recall.

    If `summary_memory_id` is given, that existing memory is used as the episode's
    retrievable handle instead of indexing a new one — used when bridging from a
    task-episode that already added a memory (avoids double-indexing the content).
    """
    db = engine._get_db()
    ensure_schema(db)
    member_ids = list(member_ids or [])
    ep_id = _uuid()
    now = _now()
    start_date, end_date = _date_bounds(db, member_ids)

    if summary_memory_id is None and index_summary and summary.strip():
        mem = engine.add(
            summary.strip(), category="episode_summary", container_tag=container_tag,
            source_conv=source_conv, event_date=end_date, check_updates=False,
        )
        summary_memory_id = mem.id

    db.execute(
        "INSERT INTO episodes (id, container_tag, source_conv, title, summary, "
        "summary_memory_id, start_date, end_date, tags, member_count, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (ep_id, container_tag, source_conv, title, summary.strip(), summary_memory_id,
         start_date, end_date, tags, len(member_ids), now, now),
    )
    if member_ids:
        db.executemany(
            "INSERT OR IGNORE INTO episode_members (episode_id, memory_id) VALUES (?,?)",
            [(ep_id, mid) for mid in member_ids],
        )
    db.commit()
    return Episode(
        id=ep_id, container_tag=container_tag, source_conv=source_conv, title=title,
        summary=summary.strip(), summary_memory_id=summary_memory_id, start_date=start_date,
        end_date=end_date, tags=tags, member_count=len(member_ids), created_at=now, updated_at=now,
    )


def get_episode(engine, episode_id: str) -> Episode | None:
    db = engine._get_db()
    ensure_schema(db)
    row = db.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    return _row_to_episode(row) if row else None


def list_episodes(engine, container_tag: str | None = None) -> list[Episode]:
    db = engine._get_db()
    ensure_schema(db)
    if container_tag:
        rows = db.execute(
            "SELECT * FROM episodes WHERE container_tag = ? ORDER BY start_date, created_at",
            (container_tag,),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM episodes ORDER BY start_date, created_at").fetchall()
    return [_row_to_episode(r) for r in rows]


def episode_for_memory(engine, memory_id: str) -> Episode | None:
    """Referenceability: given a memory, return the episode it belongs to."""
    db = engine._get_db()
    ensure_schema(db)
    row = db.execute(
        "SELECT episode_id FROM episode_members WHERE memory_id = ? LIMIT 1", (memory_id,)
    ).fetchone()
    return get_episode(engine, row["episode_id"]) if row else None


def episode_members(engine, episode_id: str) -> list[str]:
    db = engine._get_db()
    ensure_schema(db)
    rows = db.execute(
        "SELECT memory_id FROM episode_members WHERE episode_id = ?", (episode_id,)
    ).fetchall()
    return [r["memory_id"] for r in rows]


def segment_by_conversation(engine, container_tag: str = "default", summarizer=None) -> list[Episode]:
    """Naive segmentation: one episode per source_conv. Idempotent (skips convs
    that already have an episode). `summarizer(list[str]) -> str` defaults to a
    deterministic join; pass an LLM summarizer in production."""
    db = engine._get_db()
    ensure_schema(db)
    summarizer = summarizer or default_summarizer
    rows = db.execute(
        "SELECT id, content, source_conv FROM memories "
        "WHERE container_tag = ? AND is_forgotten = 0 AND category != 'episode_summary' "
        "AND source_conv IS NOT NULL ORDER BY source_conv, source_turn",
        (container_tag,),
    ).fetchall()
    by_conv: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        by_conv.setdefault(r["source_conv"], []).append((r["id"], r["content"]))

    existing = {e.source_conv for e in list_episodes(engine, container_tag) if e.source_conv}
    created = []
    for conv, items in by_conv.items():
        if conv in existing:
            continue
        member_ids = [mid for mid, _ in items]
        summary = summarizer([c for _, c in items])
        created.append(create_episode(
            engine, summary, source_conv=conv, member_ids=member_ids,
            title=str(conv), container_tag=container_tag,
        ))
    return created


def _time_key(e: Episode) -> str:
    return e.end_date or e.start_date or e.created_at or ""


def recent_episodes(engine, container_tag: str | None = None, n: int = 5) -> list[Episode]:
    """The N most recent episodes (the 'what did we just work on' query)."""
    eps = list_episodes(engine, container_tag)
    eps.sort(key=_time_key, reverse=True)
    return eps[:n]


def episodes_in_range(engine, start: str, end: str,
                      container_tag: str | None = None) -> list[Episode]:
    """All episodes overlapping the [start, end] date window ('what did I do in March')."""
    out = []
    for e in list_episodes(engine, container_tag):
        s = e.start_date or e.end_date or e.created_at
        f = e.end_date or e.start_date or e.created_at
        if s and f and s <= end and f >= start:
            out.append(e)
    out.sort(key=lambda e: e.start_date or e.created_at)
    return out


def episode_before(engine, episode_id: str, container_tag: str | None = None) -> Episode | None:
    """The episode immediately preceding `episode_id` in time ('what came before X')."""
    anchor = get_episode(engine, episode_id)
    if not anchor:
        return None
    key = anchor.start_date or anchor.created_at
    cands = [e for e in list_episodes(engine, container_tag)
             if e.id != episode_id and (e.start_date or e.created_at) < key]
    cands.sort(key=lambda e: e.start_date or e.created_at)
    return cands[-1] if cands else None


def episode_after(engine, episode_id: str, container_tag: str | None = None) -> Episode | None:
    """The episode immediately following `episode_id` in time."""
    anchor = get_episode(engine, episode_id)
    if not anchor:
        return None
    key = anchor.start_date or anchor.created_at
    cands = [e for e in list_episodes(engine, container_tag)
             if e.id != episode_id and (e.start_date or e.created_at) > key]
    cands.sort(key=lambda e: e.start_date or e.created_at)
    return cands[0] if cands else None


def recall_episodes(engine, query: str, *, container_tag: str | None = None,
                    limit: int = 5, temporal_range: tuple[str, str] | None = None,
                    recency_weight: float = 0.0) -> list[tuple[Episode, float]]:
    """Retrieve episodes by querying their indexed summaries. Returns
    (Episode, score) ranked, reusing the engine's hybrid recall."""
    db = engine._get_db()
    ensure_schema(db)
    res = engine.recall(
        query, container_tag=container_tag, limit=limit * 5,
        temporal_range=temporal_range, recency_weight=recency_weight,
    )
    out: list[tuple[Episode, float]] = []
    seen: set[str] = set()
    for sm in res.memories:
        # resolve any recalled memory that is an episode's retrievable handle —
        # works for both indexed `episode_summary` memories and bridged
        # `task_episode` memories promoted to episodes.
        row = db.execute(
            "SELECT * FROM episodes WHERE summary_memory_id = ?", (sm.memory.id,)
        ).fetchone()
        if row and row["id"] not in seen:
            seen.add(row["id"])
            out.append((_row_to_episode(row), sm.score))
        if len(out) >= limit:
            break
    return out


# ── Typed sub-events (finer granularity) ────────────────────────────────────

def _row_to_event(row) -> EpisodeEvent:
    return EpisodeEvent(
        id=row["id"], episode_id=row["episode_id"], container_tag=row["container_tag"],
        ts=row["ts"], seq=row["seq"], event_type=row["event_type"], actor=row["actor"],
        summary=row["summary"], details=row["details"], refs=json.loads(row["refs_json"] or "{}"),
        importance=row["importance"], summary_memory_id=row["summary_memory_id"],
        created_at=row["created_at"],
    )


def add_event(engine, episode_id: str, *, event_type: str, summary: str, actor: str = "",
              details: str = "", refs: dict | None = None, importance: int = 50,
              ts: str | None = None, container_tag: str = "default",
              index: bool = True) -> EpisodeEvent:
    """Append a typed event to an episode. If `index`, the event summary is stored
    as a memory so the event is content-retrievable via recall."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event_type {event_type!r}; expected one of {EVENT_TYPES}")
    db = engine._get_db()
    ensure_schema(db)
    now = _now()
    ts = ts or now
    ev_id = _uuid()
    row = db.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM episode_events WHERE episode_id = ?",
                     (episode_id,)).fetchone()
    seq = row[0] if row else 0

    summary_memory_id = None
    if index and summary.strip():
        mem = engine.add(
            f"[{event_type}] {summary.strip()}", category="episode_event",
            container_tag=container_tag, event_date=(ts or now)[:10] or None,
            check_updates=False,
        )
        summary_memory_id = mem.id

    db.execute(
        "INSERT INTO episode_events (id, episode_id, container_tag, ts, seq, event_type, "
        "actor, summary, details, refs_json, importance, summary_memory_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ev_id, episode_id, container_tag, ts, seq, event_type, actor, summary.strip(),
         details, json.dumps(refs or {}), importance, summary_memory_id, now),
    )
    db.commit()
    return EpisodeEvent(id=ev_id, episode_id=episode_id, container_tag=container_tag, ts=ts,
                        seq=seq, event_type=event_type, actor=actor, summary=summary.strip(),
                        details=details, refs=refs or {}, importance=importance,
                        summary_memory_id=summary_memory_id, created_at=now)


def get_events(engine, episode_id: str, *, event_type: str | None = None,
               min_importance: int = 0) -> list[EpisodeEvent]:
    """The typed events of an episode, in order, optionally filtered by type."""
    db = engine._get_db()
    ensure_schema(db)
    q = "SELECT * FROM episode_events WHERE episode_id = ? AND importance >= ?"
    params: list = [episode_id, min_importance]
    if event_type:
        q += " AND event_type = ?"
        params.append(event_type)
    q += " ORDER BY seq, ts"
    return [_row_to_event(r) for r in db.execute(q, params).fetchall()]


def recall_events(engine, query: str, *, container_tag: str | None = None, limit: int = 5,
                  event_type: str | None = None) -> list[tuple[EpisodeEvent, float]]:
    """Retrieve specific moments across episodes by content (and optionally type) —
    'when did the test first fail', 'the decision about X'."""
    db = engine._get_db()
    ensure_schema(db)
    res = engine.recall(query, container_tag=container_tag, limit=limit * 6)
    out, seen = [], set()
    for sm in res.memories:
        row = db.execute(
            "SELECT * FROM episode_events WHERE summary_memory_id = ?", (sm.memory.id,)
        ).fetchone()
        if not row or row["id"] in seen:
            continue
        if event_type and row["event_type"] != event_type:
            continue
        seen.add(row["id"])
        out.append((_row_to_event(row), sm.score))
        if len(out) >= limit:
            break
    return out


def events_from_task(engine, episode_id: str, *, objective: str = "",
                     tool_calls: list[dict] | None = None, response_text: str = "",
                     container_tag: str = "default", ts: str | None = None) -> list[EpisodeEvent]:
    """Derive typed events from a completed task's recorded data (NOT a live stream).

    Emits a user_message (the objective), one tool_call per tool used, and an
    agent_message (the response). To bound embedding volume, only the message
    events are content-indexed; tool_call events are stored (queryable by type and
    episode) but not embedded. Honest limit: events share the task timestamp — this
    is completion-time reconstruction, not per-turn capture with sub-turn timing.
    """
    added: list[EpisodeEvent] = []
    if objective.strip():
        added.append(add_event(engine, episode_id, event_type="user_message", actor="user",
                               summary=objective.strip()[:300], importance=80,
                               container_tag=container_tag, ts=ts, index=True))
    for tc in (tool_calls or []):
        if not isinstance(tc, dict):
            continue
        name = tc.get("tool") or tc.get("name") or tc.get("tool_name") or "tool"
        args = tc.get("arguments") or tc.get("args") or tc.get("input") or {}
        details = (json.dumps(args)[:160] if isinstance(args, (dict, list)) else str(args)[:160])
        added.append(add_event(engine, episode_id, event_type="tool_call", actor="tool",
                               summary=f"used {name}", details=details, refs={"tool": name},
                               importance=50, container_tag=container_tag, ts=ts, index=False))
    if response_text.strip():
        added.append(add_event(engine, episode_id, event_type="agent_message", actor="agent",
                               summary=response_text.strip()[:300], importance=70,
                               container_tag=container_tag, ts=ts, index=True))
    return added


__all__ = [
    "Episode", "EpisodeEvent", "EVENT_TYPES", "ensure_schema", "create_episode",
    "get_episode", "list_episodes", "episode_for_memory", "episode_members",
    "segment_by_conversation", "recall_episodes", "default_summarizer",
    "recent_episodes", "episodes_in_range", "episode_before", "episode_after",
    "add_event", "get_events", "recall_events", "events_from_task",
]
