"""Cross-agent decision & discussion threads — the when / who / why.

Episodic memory answers "when did things happen" within an agent's own sessions.
This answers the multi-agent *coordination* question: across ALL agents working in
a project, what was discussed and decided about a topic — by whom, when, and why.

It is built on episodic typed events and deliberately does NOT silo by agent: a
thread spans every agent's episodes (they share the project container_tag), each
item is attributed to the owning agent (`episode.source_agent` — the WHO), and
decision rationale (the WHY) is captured via `log_decision` and surfaced.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from charon.memory import episodic as ep


@dataclass
class ThreadItem:
    ts: str
    agent: str | None        # which agent (the WHO)
    session: str | None
    event_type: str
    what: str
    why: str = ""
    episode_id: str = ""


def log_decision(engine, episode_id: str, *, what: str, why: str = "",
                 alternatives: str = "", topic: str = "", actor: str = "agent",
                 container_tag: str = "default", ts: str | None = None,
                 importance: int = 80, auto: bool = False) -> "ep.EpisodeEvent":
    """Capture a decision with its rationale (the WHY) as a typed, indexed event,
    so it becomes part of the cross-agent thread for its topic. The decision is
    timestamped to its episode's date (so it orders correctly in the thread) unless
    an explicit `ts` is given. `auto=True` marks decisions heuristically extracted
    from agent output (decision_extract) rather than explicitly logged — kept
    auditable in details; extraction confidence arrives via `importance`."""
    if ts is None:
        e = ep.get_episode(engine, episode_id)
        ts = (e.end_date or e.start_date or e.created_at) if e else None
    details = json.dumps({"why": why, "alternatives": alternatives, "topic": topic,
                          "auto": auto})
    summary = what.strip() + (f" — because {why.strip()}" if why.strip() else "")
    return ep.add_event(engine, episode_id, event_type="decision", actor=actor,
                        summary=summary, details=details, refs={"topic": topic},
                        importance=importance, container_tag=container_tag, index=True, ts=ts)


def _agent_session(engine, episode_id, cache):
    if episode_id not in cache:
        e = ep.get_episode(engine, episode_id)
        cache[episode_id] = (e.source_agent, e.source_conv) if e else (None, None)
    return cache[episode_id]


def thread(engine, topic: str, *, container_tag: str | None = None,
           limit: int = 15, importance_weight: float = 0.5) -> list[ThreadItem]:
    """The cross-agent discussion/decision thread for `topic`: related events across
    ALL agents, chronological, attributed, with WHY on decisions."""
    hits = ep.recall_events(engine, topic, container_tag=container_tag, limit=limit,
                            importance_weight=importance_weight)
    cache: dict = {}
    items: list[ThreadItem] = []
    for ev, _score in hits:
        agent, session = _agent_session(engine, ev.episode_id, cache)
        why = ""
        if ev.event_type == "decision":
            try:
                why = json.loads(ev.details or "{}").get("why", "")
            except Exception:
                why = ""
        items.append(ThreadItem(ts=ev.ts, agent=agent, session=session,
                                event_type=ev.event_type, what=ev.summary,
                                why=why, episode_id=ev.episode_id))
    items.sort(key=lambda it: (it.ts or "", it.episode_id))
    return items


def why(engine, topic: str, *, container_tag: str | None = None,
        limit: int = 5, importance_weight: float = 0.5) -> list[dict]:
    """For a topic/decision: the decision(s), their rationale and alternatives, the
    owning agent, and the discussion that immediately preceded each (same episode)."""
    decisions = ep.recall_events(engine, topic, container_tag=container_tag,
                                 limit=limit, event_type="decision",
                                 importance_weight=importance_weight)
    cache: dict = {}
    out: list[dict] = []
    for dec, _score in decisions:
        agent, session = _agent_session(engine, dec.episode_id, cache)
        try:
            meta = json.loads(dec.details or "{}")
        except Exception:
            meta = {}
        prior = [e for e in ep.get_events(engine, dec.episode_id) if e.seq < dec.seq]
        out.append({
            "decision": dec.summary, "why": meta.get("why", ""),
            "alternatives": meta.get("alternatives", ""),
            "agent": agent, "session": session, "ts": dec.ts,
            "leading_discussion": [(e.event_type, e.summary) for e in prior][-4:],
        })
    return out


__all__ = ["ThreadItem", "log_decision", "thread", "why"]
