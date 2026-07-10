"""Timeline tool — episodic + procedural memory for Charon agents.

Where the Recall tool answers "what do I know about X" (semantic), this answers
"when/where did things happen" (episodic) and "how have I done this before"
(procedural):

  recent      — the N most recent sessions/episodes
  range       — episodes within a date window (YYYY-MM-DD .. YYYY-MM-DD)
  topic       — episodes matching a query, optionally within a window
  procedures  — learned how-to procedures applicable to a goal, ranked by success

Episodes are created automatically when tasks complete (execution_memory →
episodic), so this surfaces real past sessions, not a hand-curated log.
"""
from __future__ import annotations

from pathlib import Path

from charon.tools import ToolContext, ToolResult

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


TIMELINE_TOOL_DEF = {
    'name': 'Timeline',
    'description': (
        'Episodic + procedural memory: recall WHEN and WHERE past work happened, and '
        'reusable procedures for HOW. Use for time/context questions ("what did I work '
        'on recently / in a date range", "the session about X") and to retrieve learned '
        'how-to procedures for a goal. Complements the Recall tool (semantic facts).'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['recent', 'range', 'topic', 'events', 'procedures',
                         'thread', 'why', 'log_decision'],
                'description': 'recent | range | topic | events | procedures | thread | why | log_decision',
            },
            'n': {'type': 'number', 'description': 'For recent: how many (default 5).'},
            'start': {'type': 'string', 'description': 'For range/topic: start date YYYY-MM-DD.'},
            'end': {'type': 'string', 'description': 'For range/topic: end date YYYY-MM-DD.'},
            'query': {'type': 'string', 'description': 'For topic/events/procedures/thread/why/log_decision: '
                      'the topic, moment, goal, or decision topic.'},
            'event_type': {'type': 'string', 'description': 'For events: filter to one type '
                           '(user_message, agent_message, tool_call, tool_result, decision, '
                           'observation, system_notification).'},
            'what': {'type': 'string', 'description': 'For log_decision: what was decided.'},
            'why': {'type': 'string', 'description': 'For log_decision: the rationale (the WHY).'},
            'alternatives': {'type': 'string', 'description': 'For log_decision: alternatives considered.'},
        },
        'required': ['action'],
    },
}


def _engine(state_dir: Path):
    try:
        from charon.memory.memory_engine import MemoryEngine
        return MemoryEngine(state_dir)
    except Exception as e:
        _diag('timeline_tool', 'memory engine unavailable; Timeline tool reports episodic memory missing', error=e)
        return None


def _tag(ctx: ToolContext) -> str | None:
    if getattr(ctx, 'project_root', None):
        return f"project:{Path(ctx.project_root).resolve()}"
    return None


def _fmt_episode(e, score=None) -> str:
    when = e.start_date or e.end_date or e.created_at[:10]
    head = f"- [{when}] {e.title or e.source_conv or e.id}"
    if score is not None:
        head += f"  (score {score:.3f})"
    body = (e.summary or '').strip().replace('\n', ' ')
    return head + (f"\n    {body[:240]}" if body else '')


def execute_timeline(params: dict, ctx: ToolContext) -> ToolResult:
    if not ctx.state_dir:
        return ToolResult(content='Error: state_dir not available.', is_error=True)
    eng = _engine(ctx.state_dir)
    if eng is None:
        return ToolResult(content='Episodic memory unavailable (missing sqlite-vec / '
                                  'sentence-transformers).', is_error=True)
    try:
        from charon.memory import episodic
        from charon.memory import procedural
        from charon.agents import threads
    except Exception as e:
        return ToolResult(content=f'Episodic/procedural memory not available: {e}', is_error=True)

    action = str(params.get('action', '')).strip()
    tag = _tag(ctx)
    try:
        if action == 'recent':
            n = int(params.get('n') or 5)
            eps = episodic.recent_episodes(eng, tag, n=n)
            if not eps:
                return ToolResult(content='No episodes recorded yet.')
            return ToolResult(content=f'## {len(eps)} most recent sessions\n'
                              + '\n'.join(_fmt_episode(e) for e in eps))

        if action == 'range':
            start, end = str(params.get('start', '')), str(params.get('end', ''))
            if not (start and end):
                return ToolResult(content='Error: range needs start and end (YYYY-MM-DD).', is_error=True)
            eps = episodic.episodes_in_range(eng, start, end, tag)
            if not eps:
                return ToolResult(content=f'No sessions between {start} and {end}.')
            return ToolResult(content=f'## Sessions {start} … {end} ({len(eps)})\n'
                              + '\n'.join(_fmt_episode(e) for e in eps))

        if action == 'topic':
            query = str(params.get('query', '')).strip()
            if not query:
                return ToolResult(content='Error: topic needs a query.', is_error=True)
            start, end = params.get('start'), params.get('end')
            tr = (str(start), str(end)) if start and end else None
            hits = episodic.recall_episodes(eng, query, container_tag=tag, limit=5, temporal_range=tr)
            if not hits:
                return ToolResult(content=f'No sessions found for: {query}')
            return ToolResult(content=f'## Sessions about "{query}"\n'
                              + '\n'.join(_fmt_episode(e, s) for e, s in hits))

        if action == 'events':
            query = str(params.get('query', '')).strip()
            if not query:
                return ToolResult(content='Error: events needs a query.', is_error=True)
            et = params.get('event_type') or None
            hits = episodic.recall_events(eng, query, container_tag=tag, limit=6, event_type=et)
            if not hits:
                return ToolResult(content=f'No events found for: {query}')
            lines = [f'## Events matching "{query}"']
            for ev, _score in hits:
                when = (ev.ts or '')[:10]
                lines.append(f'- [{when}] **{ev.event_type}** ({ev.actor or "?"}): {ev.summary[:200]}')
            return ToolResult(content='\n'.join(lines))

        if action == 'procedures':
            goal = str(params.get('query', '')).strip()
            if not goal:
                return ToolResult(content='Error: procedures needs a goal query.', is_error=True)
            hits = procedural.recall_procedures(eng, goal, container_tag=tag, limit=5)
            if not hits:
                return ToolResult(content=f'No learned procedures for: {goal}')
            lines = [f'## Procedures for "{goal}"']
            for p, _score in hits:
                rate = procedural.success_rate(p)
                lines.append(f'- **{p.name}** (success {p.success_count}/{p.success_count + p.failure_count}, '
                             f'rate {rate:.2f})')
                lines.append('    steps: ' + ' → '.join(p.steps[:8]))
            return ToolResult(content='\n'.join(lines))

        if action == 'thread':
            topic = str(params.get('query', '')).strip()
            if not topic:
                return ToolResult(content='Error: thread needs a query (topic).', is_error=True)
            items = threads.thread(eng, topic, container_tag=tag, limit=15)
            if not items:
                return ToolResult(content=f'No cross-agent discussion/decisions for: {topic}')
            lines = [f'## Thread: "{topic}" — when / who / why (across agents)']
            for it in items:
                lines.append(f"- [{(it.ts or '')[:10]}] **{it.agent or '?'}** · {it.event_type}: {it.what[:160]}")
                if it.why:
                    lines.append(f"    WHY: {it.why[:220]}")
            return ToolResult(content='\n'.join(lines))

        if action == 'why':
            topic = str(params.get('query', '')).strip()
            if not topic:
                return ToolResult(content='Error: why needs a query (topic/decision).', is_error=True)
            decs = threads.why(eng, topic, container_tag=tag, limit=5)
            if not decs:
                return ToolResult(content=f'No recorded decisions for: {topic}')
            lines = [f'## Why: "{topic}"']
            for w in decs:
                lines.append(f"- **{w['decision'][:200]}**")
                lines.append(f"    decided by {w['agent'] or '?'} on {(w['ts'] or '')[:10]}")
                if w['alternatives']:
                    lines.append(f"    alternatives considered: {w['alternatives'][:160]}")
                if w['leading_discussion']:
                    lines.append("    led up to by: " + "; ".join(
                        f"{t}: {s[:50]}" for t, s in w['leading_discussion']))
            return ToolResult(content='\n'.join(lines))

        if action == 'log_decision':
            what = str(params.get('what', '')).strip()
            if not what:
                return ToolResult(content='Error: log_decision needs `what` (the decision).', is_error=True)
            sess = getattr(ctx, 'conversation_id', '') or 'session'
            epi = episodic.get_or_create_episode_for_session(
                eng, source_conv=sess, container_tag=tag or 'default',
                source_agent=getattr(ctx, 'agent_id', None))
            threads.log_decision(eng, epi.id, what=what, why=str(params.get('why', '')),
                                 alternatives=str(params.get('alternatives', '')),
                                 topic=str(params.get('query', '')), container_tag=tag or 'default')
            return ToolResult(content=f'Logged decision: {what}')

        return ToolResult(content=f'Unknown action: {action}', is_error=True)
    except Exception as e:
        return ToolResult(content=f'Timeline error: {e}', is_error=True)
    finally:
        eng.close()
