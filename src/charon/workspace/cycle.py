"""Overseer cycle: collect what changed, build the digest, deliver it.

Mirrors Acheron's ``src/overseer.js`` (``queueEvent`` / ``injectDigest`` /
``scheduleDigest``) and ``src/overseer/prompt.js`` (``digestMessage``) so a
digest built here is indistinguishable from one Acheron pastes into an
overseer's terminal.  The digest is the trigger for one **cycle**; the
overseer (a Charon agent with the ``overseer`` skill, or an external Claude
Code session) runs the cycle protocol and ends it with ``OverseerReport``.

Pure functions plus a small runner.  Delivery is a callable so the loop can
route a digest into a session (``deliver_to_session``) or enqueue it as an
``agent_task`` for a native Charon overseer agent (``deliver_to_agent``).
A failed delivery never consumes the events: cursors and status snapshots are
persisted only after ``deliver`` returns True.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from charon.workspace.policy import redact
from charon.workspace.projections import build_projection

SHELL_RE = re.compile(r'^-?(zsh|bash|fish|sh|dash|tcsh|ksh|login)$')
NOISE_EVENTS = {'record.created', 'record.updated', 'cycle.started', 'cycle.report', 'evidence.attached', 'gate.opened'}
DEFAULT_MAX_CYCLES_PER_HOUR = 12
QUIET_MS = 15_000          # user typed in the overseer's pane → hold
HEAD_CHARS = 160

EXT_CYCLE_COUNT = 'cycle_count'
EXT_CURSOR = 'cycle_cursor'              # {replica_id: last replica_sequence digested}
EXT_STAMPS = 'cycle_stamps'              # [ms] of delivered cycles (hourly cap)
EXT_SESSION_STATUS = 'cycle_session_status'   # {session_id: status} last observed via transport


class SessionTransport(Protocol):
    def list_sessions(self) -> list[dict]: ...
    def read(self, session_id: str, mode: str = 'last_message', lines: int = 2000) -> str: ...
    def send(self, session_id: str, text: str, *, enter: bool = True) -> str: ...
    def foreground(self, session_id: str) -> str | None: ...


# ── helpers ───────────────────────────────────────────────────────────────

def _now_ms(now: Callable[[], float] | None = None) -> float:
    return float(now()) if now else datetime.now(timezone.utc).timestamp() * 1000.0


def time_ago(delta_ms: float) -> str:
    s = max(0.0, delta_ms / 1000.0)
    if s < 60:
        return f'{round(s)}s ago'
    if s < 3600:
        return f'{round(s / 60)}m ago'
    if s < 86400:
        return f'{round(s / 3600)}h ago'
    return f'{round(s / 86400)}d ago'


def _parse_iso_ms(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000.0
    except ValueError:
        return None


def _callsign(rec: dict | None, fallback: str = '?') -> str:
    if not rec:
        return fallback
    ext = rec.get('extensions') or {}
    return ext.get('callsign') or ext.get('title') or rec.get('callsign') or rec.get('title') or rec.get('id') or fallback


def _session_name(store, session_id: str | None) -> str:
    if not session_id:
        return '?'
    return _callsign(store.get('session', session_id), session_id)


def _head(text: str) -> str:
    return ' '.join(redact(text or '').split())[:HEAD_CHARS]


def _task_tag(store, session_id: str | None) -> str:
    rec = store.get('session', session_id) if session_id else None
    active = ((rec or {}).get('extensions') or {}).get('active_task_id')
    return f' ({active})' if active else ''


# ── event collection ──────────────────────────────────────────────────────

def collect_events(store, since_seq: int | dict[str, int] | None = None, *, transport: SessionTransport | None = None,
                   now: Callable[[], float] | None = None) -> tuple[list[dict], dict[str, int], dict[str, str]]:
    """Digest lines since the cursor.

    Returns ``(lines, cursor, session_status)``: ``lines`` are ``{'at': ms, 'kind', 'text'}``
    (``text`` mirrors Acheron's wording), ``cursor`` is the new per-replica sequence map and
    ``session_status`` the transport-observed statuses — both to persist **after** a
    successful delivery.  ``since_seq`` may be an int (this replica only) or a cursor map.
    """
    cursor: dict[str, int] = {}
    if isinstance(since_seq, dict):
        cursor.update({k: int(v) for k, v in since_seq.items()})
    elif isinstance(since_seq, int):
        cursor[store.replica_id] = since_seq
    lines: list[dict] = []
    new_cursor = dict(cursor)

    for ev in store.events:
        rid = ev.get('replica_id') or store.replica_id
        seq = int(ev.get('replica_sequence') or 0)
        if seq <= cursor.get(rid, 0):
            continue
        new_cursor[rid] = max(new_cursor.get(rid, 0), seq)
        line = _line_for_event(store, ev, transport)
        if line:
            lines.append({'at': _parse_iso_ms(ev.get('occurred_at')) or _now_ms(now), **line})

    statuses: dict[str, str] = {}
    if transport is not None:
        prev = store.get_extension(EXT_SESSION_STATUS) or {}
        for s in transport.list_sessions() or []:
            sid = s.get('id')
            if not sid:
                continue
            status = s.get('status') or 'idle'
            statuses[sid] = status
            before = prev.get(sid)
            if before == status or (s.get('role') == 'overseer'):
                continue
            name = s.get('callsign') or s.get('title') or sid
            tag = _task_tag(store, sid)
            if before == 'running' and status == 'idle':
                head = ''
                try:
                    head = _head(transport.read(sid, 'last_message'))
                except Exception:
                    head = ''
                lines.append({'at': _now_ms(now), 'kind': 'finished',
                              'text': f'{name}{tag}: finished — "{head}"' if head else f'{name}{tag}: finished — (no message captured)'})
            elif status == 'waiting':
                head = ''
                try:
                    head = _head(transport.read(sid, 'screen', 40))
                except Exception:
                    head = ''
                lines.append({'at': _now_ms(now), 'kind': 'waiting',
                              'text': f'{name}{tag}: waiting for approval — "{head}"' if head else f'{name}{tag}: waiting for approval — (no message captured)'})
            elif status == 'error':
                lines.append({'at': _now_ms(now), 'kind': 'error', 'text': f'{name}{tag}: error'})
            elif status == 'detached' and before not in (None, 'detached', 'starting'):
                lines.append({'at': _now_ms(now), 'kind': 'detached', 'text': f'{name}{tag}: session detached/gone'})
    lines.sort(key=lambda l: l['at'])
    return lines, new_cursor, statuses


def _line_for_event(store, ev: dict, transport: SessionTransport | None) -> dict | None:
    et = ev.get('event_type') or ''
    payload = ev.get('payload') or {}
    refs = ev.get('subject_refs') or []
    ref = refs[0] if refs else {}
    if et in NOISE_EVENTS:
        return None
    if et == 'record.transitioned':
        rtype, rid, to = ref.get('record_type'), ref.get('id'), payload.get('to')
        if rtype == 'work_item':
            return {'kind': 'work', 'text': f'work item {rid} → {to}'}
        if rtype == 'task' and to in ('succeeded', 'failed', 'cancelled'):
            task = store.get('task', rid) or {}
            who = _session_name(store, task.get('session_id'))
            return {'kind': 'task', 'text': f'task {rid} ({who}) → {to}'}
        if rtype == 'lease' and to == 'expired':
            lease = store.get('lease', rid) or {}
            sel = (lease.get('resource') or {}).get('selector', '?')
            return {'kind': 'lease', 'text': f'lease expired: {sel} ({_session_name(store, lease.get("owner_session_id"))})'}
        return None
    if et == 'gate.decided':
        gate = store.get('gate', ref.get('id')) or {}
        answer = payload.get('answer')
        if isinstance(answer, dict):
            answer = answer.get('answer', answer)
        q = str(gate.get('question') or '')[:80]
        return {'kind': 'gate', 'text': f'gate {payload.get("kind") or gate.get("kind")} "{q}" decided by user: {answer}'}
    if et == 'lease.conflict':
        me = _session_name(store, payload.get('session_id'))
        holders = sorted({_session_name(store, c.get('owner_session_id')) for c in payload.get('conflicts') or []})
        sels = sorted({(c.get('scope') or {}).get('selector', '?') for c in payload.get('conflicts') or []})
        return {'kind': 'conflict', 'text': f'lease conflict: {me} vs {", ".join(holders) or "?"} on {", ".join(sels)}'}
    if et == 'session.status':
        sid = ref.get('id')
        rec = store.get('session', sid) or {}
        if (rec.get('extensions') or {}).get('role') == 'overseer':
            return None
        name = _session_name(store, sid)
        tag = _task_tag(store, sid)
        frm, to = payload.get('from'), payload.get('to')
        if frm == 'active' and to == 'idle':
            head = (rec.get('extensions') or {}).get('last_message_head') or ''
            if transport is not None and not head:
                try:
                    head = _head(transport.read(sid, 'last_message'))
                except Exception:
                    head = ''
            return {'kind': 'finished', 'text': f'{name}{tag}: finished — "{head}"' if head else f'{name}{tag}: finished — (no message captured)'}
        if to == 'failed':
            return {'kind': 'error', 'text': f'{name}{tag}: error'}
        if to == 'disconnected' and frm not in (None, 'starting', 'disconnected'):
            return {'kind': 'detached', 'text': f'{name}{tag}: session detached/gone'}
        return None
    if et == 'session.spawned':
        return {'kind': 'spawned', 'text': f'spawned {payload.get("role") or "session"} ({str(ref.get("id") or "")[8:16]}) — prompt queued'}
    if et == 'decision':
        return None
    return None


# ── digest ────────────────────────────────────────────────────────────────

def build_digest(store, cycle: int, workspace_name: str, lines: list[dict], now: Callable[[], float] | None = None,
                 projection: dict | None = None) -> str:
    """Exactly Acheron's ``digestMessage`` text."""
    p = projection or build_projection(store)
    active = sum(1 for t in p.get('tasks') or [] if t.get('status') in ('claimed', 'running', 'waiting'))
    gates = sum(1 for g in p.get('gates') or [] if g.get('status') == 'open')
    now_ms = _now_ms(now)
    when = datetime.fromtimestamp(now_ms / 1000.0).strftime('%H:%M')
    head = (f'[acheron cycle {cycle} · {when} · workspace {workspace_name} · {active} active task{"" if active == 1 else "s"}'
            f' · {gates} gate{"" if gates == 1 else "s"} open]')
    body = '\n'.join(f'- {l["text"]} ({time_ago(now_ms - float(l.get("at") or now_ms))})' for l in lines) if lines else '- (no new events)'
    return f'{head}\n{body}\nRun a cycle.'


# ── runner ────────────────────────────────────────────────────────────────

def run_cycle(store, *, deliver: Callable[[str], bool], transport: SessionTransport | None = None,
              cadence: dict | None = None, overseer_session_id: str | None = None, workspace_name: str | None = None,
              force: bool = False, now: Callable[[], float] | None = None) -> dict[str, Any]:
    """One digest attempt.  Returns ``{'delivered', 'held', 'cycle', 'lines', 'text'}``.

    Hold rules (Acheron's): nothing new (unless ``force``), the overseer pane's foreground
    is a shell, the overseer session is running/starting, the user typed there within 15 s
    (when the transport reports ``user_typed_ago_ms``), or the hourly cap is reached.
    A failed ``deliver`` keeps everything for the next attempt.
    """
    cadence = cadence or {}
    max_per_hour = int(cadence.get('max_cycles_per_hour') or DEFAULT_MAX_CYCLES_PER_HOUR)
    now_ms = _now_ms(now)
    cursor = store.get_extension(EXT_CURSOR) or {}
    lines, new_cursor, statuses = collect_events(store, cursor, transport=transport, now=now)
    texts = [l['text'] for l in lines]
    cycle = int(store.get_extension(EXT_CYCLE_COUNT) or 0) + 1
    base = {'delivered': False, 'held': None, 'cycle': cycle - 1, 'lines': texts, 'text': None}

    if not lines and not force:
        return {**base, 'held': 'no new events'}

    if transport is not None and overseer_session_id:
        try:
            fg = transport.foreground(overseer_session_id) or ''
        except Exception:
            fg = ''
        if fg and SHELL_RE.match(fg.strip()):
            return {**base, 'held': f'overseer offline ({fg.strip()} in the foreground)'}
        ov = next((s for s in (transport.list_sessions() or []) if s.get('id') == overseer_session_id), None)
        if ov and ov.get('status') in ('running', 'starting') and not force:
            return {**base, 'held': 'overseer busy'}
        typed = (ov or {}).get('user_typed_ago_ms')
        if isinstance(typed, (int, float)) and typed < QUIET_MS and not force:
            return {**base, 'held': 'user typing in the overseer session'}

    stamps = [s for s in (store.get_extension(EXT_STAMPS) or []) if isinstance(s, (int, float)) and now_ms - s < 3_600_000]
    if len(stamps) >= max_per_hour and not force:
        return {**base, 'held': f'hourly cap ({max_per_hour} cycles/hour)'}

    name = workspace_name or (store.workspace.get('title') if isinstance(store.workspace, dict) else None) or store.workspace_id
    text = build_digest(store, cycle, name, lines, now=now)
    ok = False
    try:
        ok = bool(deliver(text))
    except Exception:
        ok = False
    if not ok:
        return {**base, 'held': 'delivery failed', 'text': text}

    store.append_event('cycle.started', actor={'kind': 'system', 'id': 'system.charon'}, payload={'cycle': cycle, 'lines': texts})
    # the cycle.started event itself must not show up in the next digest
    new_cursor[store.replica_id] = max(new_cursor.get(store.replica_id, 0), store.seq)
    store.set_extension(EXT_CYCLE_COUNT, cycle)
    store.set_extension(EXT_CURSOR, new_cursor)
    store.set_extension(EXT_STAMPS, stamps + [now_ms])
    if transport is not None:
        store.set_extension(EXT_SESSION_STATUS, statuses)
    return {'delivered': True, 'held': None, 'cycle': cycle, 'lines': texts, 'text': text}


# ── delivery strategies ───────────────────────────────────────────────────

def deliver_to_session(transport: SessionTransport, session_id: str) -> Callable[[str], bool]:
    """Type the digest into the overseer's terminal (bracketed paste is the transport's job)."""
    def _deliver(text: str) -> bool:
        transport.send(session_id, text, enter=True)
        return True
    return _deliver


def deliver_to_agent(state_dir: Path | str, owner_agent_id: str, *, project: str | None = None,
                     correlation_id: str | None = None) -> Callable[[str], bool]:
    """Enqueue the digest as an agent_task so a native Charon overseer runs the cycle in-process."""
    from charon.conversation.conversation_runtime import enqueue_agent_task

    def _deliver(text: str) -> bool:
        task = enqueue_agent_task(Path(state_dir), owner_agent_id=owner_agent_id, title='overseer cycle',
                                  instruction=text + '\nFollow the overseer skill.', project=project,
                                  correlation_id=correlation_id)
        return bool(task and task.get('id'))
    return _deliver


def build_transport(spec: dict | None):
    """``{'kind': 'none'|'auto'|'tmux'|'charond', ...}`` → transport or None (lazy import)."""
    kind = (spec or {}).get('kind') or 'none'
    if kind == 'none':
        return None
    try:
        from charon.workspace import transport as T
    except Exception:
        return None
    try:
        if kind == 'tmux':
            return T.LocalTmuxTransport(socket=(spec or {}).get('socket') or 'acheron')
        if kind == 'charond':
            return T.CharondTransport(sock_path=(spec or {}).get('sock_path'))
        return T.AutoTransport()
    except Exception:
        return None
