"""Agent task ledger — unified view of everything an agent has done.

Reads from multiple sources (tasks, inbox, working memory, goals) and
produces a single chronological list of concise task summaries.

Used by:
- The rear-view pane in the dashboard (per agent)
- The toggleable history sidebar in the chat view
- The /history command
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _parse_ts(ts: str | None) -> float:
    """Parse an ISO timestamp to epoch seconds. Returns 0 on failure."""
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts)
        return dt.timestamp()
    except Exception:
        return 0.0


def _fmt_ts(ts: str | None) -> str:
    """Format timestamp as short date+time."""
    if not ts:
        return ''
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime('%b %d %H:%M')
    except Exception:
        return ts[:16] if ts else ''


def get_agent_ledger(
    state_dir: Path,
    agent_id: str,
    *,
    limit: int = 100,
    include_pending: bool = True,
) -> list[dict]:
    """Build a chronological ledger of tasks for an agent.

    Each entry:
    {
        'ts': '2026-03-20T14:23:00Z',
        'ts_short': 'Mar 20 14:23',
        'status': 'completed' | 'failed' | 'pending' | 'in_progress',
        'title': 'Fixed auth bug in login.py',
        'task_id': 'task-abc123',
        'task_type': 'agent_task',
        'source': 'task_queue' | 'working_memory' | 'inbox',
    }

    Sorted newest first. Deduplicated by task_id.
    """
    entries: dict[str, dict] = {}  # keyed by task_id or synthetic key

    # Source 1: Task queue (SQLite)
    try:
        from charon.infra.store_adapter import get_db, task_list
        db = get_db(state_dir)
        tasks = task_list(db, owner_agent_id=agent_id, limit=limit * 2)
        for t in tasks:
            tid = t.get('id', '')
            title = t.get('result_summary') or t.get('title') or t.get('instruction') or ''
            title = title.strip()[:200]
            if not title:
                continue
            ts = t.get('completed_at') or t.get('started_at') or t.get('created_at') or ''
            entries[tid] = {
                'ts': ts,
                'ts_short': _fmt_ts(ts),
                'status': t.get('status', 'pending'),
                'title': title,
                'task_id': tid,
                'task_type': t.get('task_type', ''),
                'source': 'task_queue',
            }
    except Exception:
        pass

    # Source 2: Working memory notes (fills gaps when tasks aren't in SQLite yet)
    try:
        from charon.infra.store_adapter import get_db, agent_memory_get
        db = get_db(state_dir)
        memory = agent_memory_get(db, agent_id)
        if memory and memory.get('notes'):
            for note in memory['notes']:
                tid = note.get('task_id', '')
                if tid and tid in entries:
                    continue  # already have this from task queue
                summary = note.get('summary', '').strip()[:200]
                if not summary:
                    continue
                ts = note.get('ts', '')
                key = tid or f'mem-{ts}'
                entries[key] = {
                    'ts': ts,
                    'ts_short': _fmt_ts(ts),
                    'status': 'completed',
                    'title': summary,
                    'task_id': tid,
                    'task_type': 'memory_note',
                    'source': 'working_memory',
                }
    except Exception:
        pass

    # Also try JSON fallback for working memory
    if not entries:
        try:
            mem_path = state_dir / 'agents' / agent_id / 'working_memory.json'
            if mem_path.exists():
                memory = json.loads(mem_path.read_text())
                for note in (memory.get('notes') or []):
                    summary = note.get('summary', '').strip()[:200]
                    if not summary:
                        continue
                    ts = note.get('ts', '')
                    tid = note.get('task_id', '')
                    key = tid or f'mem-{ts}'
                    if key not in entries:
                        entries[key] = {
                            'ts': ts,
                            'ts_short': _fmt_ts(ts),
                            'status': 'completed',
                            'title': summary,
                            'task_id': tid,
                            'task_type': 'memory_note',
                            'source': 'working_memory',
                        }
        except Exception:
            pass

    # Sort by timestamp (newest first) and limit
    result = sorted(entries.values(), key=lambda e: _parse_ts(e.get('ts')), reverse=True)

    if not include_pending:
        result = [e for e in result if e.get('status') not in ('pending',)]

    return result[:limit]


def get_agent_ledger_summary(
    state_dir: Path,
    agent_id: str,
    *,
    limit: int = 50,
) -> dict:
    """Get a ledger with summary stats.

    Returns:
    {
        'entries': [...],
        'stats': {
            'total': 42,
            'completed': 38,
            'failed': 2,
            'pending': 2,
        }
    }
    """
    entries = get_agent_ledger(state_dir, agent_id, limit=limit)
    stats = {'total': len(entries), 'completed': 0, 'failed': 0, 'pending': 0, 'in_progress': 0}
    for e in entries:
        s = e.get('status', '')
        if s in stats:
            stats[s] += 1

    return {'entries': entries, 'stats': stats}


def format_ledger_text(entries: list[dict], *, max_lines: int = 30) -> str:
    """Format ledger entries as readable text for the history pane."""
    if not entries:
        return '(no task history)'

    icons = {
        'completed': '✓',
        'failed': '✗',
        'pending': '○',
        'in_progress': '◆',
    }
    lines = []
    for e in entries[:max_lines]:
        icon = icons.get(e.get('status', ''), '·')
        ts = e.get('ts_short', '')
        title = e.get('title', '')[:80]
        lines.append(f'{icon} {ts}  {title}')

    if len(entries) > max_lines:
        lines.append(f'  ... and {len(entries) - max_lines} more')

    return '\n'.join(lines)
