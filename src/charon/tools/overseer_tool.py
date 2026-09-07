"""Overseer tools — the 20 operations of ``docs/contracts/overseer-tools.json`` as
Charon tools over ``charon.workspace`` (records) and a ``SessionTransport`` (sessions).

Semantics mirror Acheron's dispatcher (``src/overseer.js``) handler for handler so an
overseer works the same from Acheron's endpoint, from ``charon.mcp_server --profile
overseer`` (which exposes these under their ``acheron_*`` contract names), or from a
Charon agent calling the Charon names (``WorkDispatch`` …).

Context (``ToolContext``):
  metadata['workspace_root']  the workspace directory (default: <state_dir>/projects/<slug>/workspace)
  metadata['transport']       a SessionTransport (default: AutoTransport — charond if present, else tmux -L acheron)
  metadata['policy']          {'paused', 'forward_approvals', 'spawn': allow|confirm|deny, 'max_blocks', 'wired': 'all' | [session ids],
                               'user_typed_ago_ms': {session id: ms}}   (defaults: not paused, no forwarding, allow, 8, 'all')
  agent_id                    the overseer's actor id (``agent.<agent_id>``)
"""
from __future__ import annotations

import json
import os
import re
import socket as _socket
import time
from functools import partial
from pathlib import Path
from typing import Any, Callable

from charon.tools import ToolContext, ToolResult
from charon.workspace import (
    GuardFailed, IllegalTransition, LeaseConflict, NotFound, RevisionConflict, ValidationError, WorkspaceStore,
    evaluate_send_policy, evaluate_spawn_policy, redact, sha256_hex,
)
from charon.workspace.records import slugify, unmet_required_criteria
from charon.workspace.projections import flatten_tree

VERSION = '0.1.0'
MAX_OPEN_QUESTIONS = 3
MAX_MUTATIONS_PER_WINDOW = 60
MUTATION_WINDOW_S = 600
LEASE_TTL_MS = 30 * 60 * 1000

_CONTRACT_ENV = 'CHARON_OVERSEER_CONTRACT'


# ── contract → TOOL_DEFs ───────────────────────────────────────────────────

def contract_path() -> Path:
    raw = os.environ.get(_CONTRACT_ENV)
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[3] / 'docs' / 'contracts' / 'overseer-tools.json'


def load_contract() -> list[dict[str, Any]]:
    with open(contract_path(), encoding='utf-8') as fh:
        return list(json.load(fh)['tools'])


CONTRACT = load_contract()
MCP_TO_CHARON = {t['name']: t['charon_name'] for t in CONTRACT}
CHARON_TO_MCP = {v: k for k, v in MCP_TO_CHARON.items()}

OVERSEER_TOOL_DEFS = [
    # Charon's registry requires an explicit `required` list on every schema (tests/test_tools.py).
    {'name': t['charon_name'], 'description': t['description'], 'input_schema': {**t['inputSchema'], 'required': list(t['inputSchema'].get('required', []))}}
    for t in CONTRACT
]

MUTATING = {
    'acheron_work_create', 'acheron_work_update', 'acheron_work_transition', 'acheron_dispatch', 'acheron_intervene',
    'acheron_interrupt', 'acheron_checkpoint', 'acheron_evidence_attach', 'acheron_lease_heartbeat', 'acheron_lease_release',
    'acheron_spawn', 'acheron_propose', 'acheron_decide', 'acheron_label', 'acheron_report', 'acheron_ask_user',
}


class OverseerError(RuntimeError):
    """Turned into an ``is_error`` ToolResult with Acheron's error phrasing."""


# ── context resolution ─────────────────────────────────────────────────────

_STORES: dict[str, WorkspaceStore] = {}
_MUTATIONS: dict[str, list[float]] = {}


def _meta(ctx: ToolContext) -> dict[str, Any]:
    return ctx.metadata or {}


def workspace_root(ctx: ToolContext) -> Path:
    meta = _meta(ctx)
    if meta.get('workspace_root'):
        return Path(str(meta['workspace_root']))
    state = ctx.state_dir or (Path(ctx.project_root) / '.charon_state')
    slug = meta.get('workspace_slug') or slugify(Path(ctx.project_root).name) or 'workspace'
    return Path(state) / 'projects' / slug / 'workspace'


def _replica_id() -> str:
    host = re.sub(r'[^A-Za-z0-9_.-]', '-', _socket.gethostname().split('.')[0] or 'local')[:40]
    return f'replica.charon.{host}'


def open_store(ctx: ToolContext) -> WorkspaceStore:
    root = workspace_root(ctx)
    key = str(root)
    store = _STORES.get(key)
    if store is not None and store.root == root:
        return store
    ws_json = root / 'workspace.json'
    slug = slugify(Path(ctx.project_root).name) or 'workspace'
    workspace_id = f'workspace.charon.{slug}'
    title = Path(ctx.project_root).name
    if ws_json.exists():
        try:
            existing = json.loads(ws_json.read_text(encoding='utf-8'))
            workspace_id = existing.get('id') or workspace_id
            slug = existing.get('slug') or slug
            title = existing.get('title') or title
        except (OSError, ValueError):
            pass
    root.mkdir(parents=True, exist_ok=True)
    store = WorkspaceStore.open(root, workspace_id=workspace_id, replica_id=_meta(ctx).get('replica_id') or _replica_id(),
                                slug=slug, title=title, roots=[{'kind': 'directory', 'locator': str(ctx.project_root)}])
    _STORES[key] = store
    return store


def reset_store_cache() -> None:
    _STORES.clear()
    _MUTATIONS.clear()


def transport_for(ctx: ToolContext) -> Any:
    t = _meta(ctx).get('transport')
    if t is not None:
        return t
    from charon.workspace.transport import AutoTransport
    t = AutoTransport()
    _meta(ctx)['transport'] = t if ctx.metadata is not None else t
    return t


def policy_for(ctx: ToolContext) -> dict[str, Any]:
    p = dict(_meta(ctx).get('policy') or {})
    p.setdefault('paused', False)
    p.setdefault('forward_approvals', False)
    p.setdefault('spawn', 'allow')
    p.setdefault('max_blocks', 8)
    p.setdefault('wired', 'all')
    p.setdefault('user_typed_ago_ms', {})
    return p


def actor_for(ctx: ToolContext) -> dict[str, str]:
    return {'kind': 'agent', 'id': f"agent.{ctx.agent_id or 'overseer'}"}


SYSTEM = {'kind': 'system', 'id': 'system.charon'}


# ── sessions ───────────────────────────────────────────────────────────────

_STATUS_TO_SESSION = {'starting': 'starting', 'running': 'active', 'idle': 'idle', 'waiting': 'idle', 'done': 'idle',
                      'error': 'failed', 'detached': 'disconnected', 'disconnected': 'disconnected'}


def _is_overseer_session(s: dict[str, Any], ctx: ToolContext) -> bool:
    role = str(s.get('role') or '').lower()
    callsign = str(s.get('callsign') or '').lower()
    return role == 'overseer' or callsign == 'overseer' or s.get('agent_id') == ctx.agent_id


def sync_sessions(store: WorkspaceStore, sessions: list[dict[str, Any]], ctx: ToolContext) -> None:
    for s in sessions:
        sid = s['id']
        ext = {
            'block_id': s.get('block_id'), 'name': s.get('name'), 'title': s.get('title'), 'callsign': s.get('callsign'),
            'agent': s.get('agent'), 'role': 'overseer' if _is_overseer_session(s, ctx) else s.get('role'),
            'acheron_status': s.get('status'), 'host_ref': s.get('host_ref'), 'cwd': s.get('cwd'),
        }
        status = _STATUS_TO_SESSION.get(str(s.get('status')), 'idle')
        rec = store.get('session', sid)
        if rec is None:
            store.create('session', {
                'id': sid, 'kind': 'coordinator' if ext['role'] == 'overseer' else 'worker', 'status': status,
                'actor': {'kind': 'agent', 'id': f"agent.{s.get('block_id') or s.get('name') or sid[8:]}"},
                'host_ref': s.get('host_ref') or 'local', 'task_ids': [], 'extensions': {k: v for k, v in ext.items() if v is not None},
            }, actor=SYSTEM)
        else:
            e = rec.get('extensions') or {}
            changed = rec.get('status') != status or any(e.get(k) != v for k, v in ext.items() if v is not None)
            if changed:
                store.set_session_status(sid, status, actor=SYSTEM, extensions={k: v for k, v in ext.items() if v is not None})


def resolve_session(ctx: ToolContext, ref: Any, sessions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if not ref:
        raise OverseerError('session is required')
    r = str(ref).strip()
    transport = transport_for(ctx)
    sessions = sessions if sessions is not None else transport.list_sessions()
    bare = r[len('session.'):] if r.startswith('session.') else r
    for s in sessions:
        if s['id'] == r or s['id'] == f'session.{bare}' or s.get('block_id') == bare or s.get('name') == bare:
            return s
    lc = r.lower()
    exact = [s for s in sessions if str(s.get('callsign', '')).lower() == lc or str(s.get('title', '')).lower() == lc]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise OverseerError(f'ambiguous session "{r}": ' + ', '.join(f"{s['callsign']} ({s['id']})" for s in exact))
    pre = [s for s in sessions if str(s.get('callsign', '')).lower().startswith(lc) or str(s.get('title', '')).lower().startswith(lc)
           or str(s.get('block_id') or '').startswith(bare) or str(s.get('name') or '').startswith(bare)]
    if len(pre) == 1:
        return pre[0]
    if len(pre) > 1:
        raise OverseerError(f'ambiguous session "{r}": ' + ', '.join(f"{s['callsign']} ({s['id']})" for s in pre))
    raise OverseerError(f'no session matches "{r}" — use acheron_fleet for callsigns')


def _wired(policy: dict[str, Any], session: dict[str, Any]) -> bool:
    w = policy.get('wired', 'all')
    if w == 'all' or w is True:
        return True
    if isinstance(w, (list, tuple, set)):
        return session['id'] in w or (session.get('callsign') in w) or (session.get('block_id') in w)
    return False


def check_send_policy(ctx: ToolContext, action: str, session: dict[str, Any], *, content: str = '', force: bool = False) -> None:
    policy = policy_for(ctx)
    typed = (policy.get('user_typed_ago_ms') or {}).get(session['id'])
    verdict = evaluate_send_policy(
        action=action, paused=bool(policy.get('paused')), is_self=_is_overseer_session(session, ctx),
        wired=_wired(policy, session), target_status=session.get('status'), user_typed_ago_ms=typed,
        content=content, force=force, forward_approvals=bool(policy.get('forward_approvals')),
        callsign=str(session.get('callsign') or session['id']),
    )
    if not verdict.get('ok'):
        raise OverseerError(f"policy.{verdict['code']}: {verdict['reason']}")


def _read_session(ctx: ToolContext, session: dict[str, Any], mode: str = 'last_message', lines: int = 2000) -> str:
    try:
        return redact(transport_for(ctx).read(session['id'], mode, lines))
    except NotImplementedError:
        raise
    except Exception as exc:  # transport problems are findings, not crashes
        raise OverseerError(f"cannot read {session.get('callsign') or session['id']}: {exc}") from exc


def _send(ctx: ToolContext, session: dict[str, Any], text: str, *, enter: bool = True) -> str:
    try:
        return transport_for(ctx).send(session['id'], text, enter=enter)
    except Exception as exc:
        raise OverseerError(f"{session.get('callsign') or session['id']} has no live terminal: {exc}") from exc


def _norm_scopes(scopes: Any) -> list[dict[str, Any]]:
    out = []
    for s in scopes or []:
        if not isinstance(s, dict) or not s.get('selector'):
            continue
        out.append({'kind': s.get('kind') or 'path', 'selector': str(s['selector']), 'access': s.get('access') or 'write',
                    'recursive': s.get('recursive', True) is not False})
    return out


def _flat_items(store: WorkspaceStore) -> list[dict[str, Any]]:
    return flatten_tree(store.projection().get('work_items') or [])


def _short(n: int = 10) -> str:
    return os.urandom(16).hex()[:n]


def _rate_limit(store: WorkspaceStore) -> None:
    now = time.time()
    stamps = [t for t in _MUTATIONS.get(str(store.root), []) if now - t < MUTATION_WINDOW_S]
    if len(stamps) >= MAX_MUTATIONS_PER_WINDOW:
        _MUTATIONS[str(store.root)] = stamps
        raise OverseerError(f'rate limit: {MAX_MUTATIONS_PER_WINDOW} mutating calls in 10 minutes — report and stop')
    stamps.append(now)
    _MUTATIONS[str(store.root)] = stamps


# ── handlers (MCP names) ───────────────────────────────────────────────────

def h_fleet(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    transport = transport_for(ctx)
    sessions = transport.list_sessions()
    sync_sessions(store, sessions, ctx)
    policy = policy_for(ctx)
    out = []
    for s in sessions:
        rec = store.get('session', s['id']) or {}
        ext = rec.get('extensions') or {}
        out.append({
            'id': s['id'], 'callsign': s.get('callsign'), 'title': s.get('title'), 'kind': s.get('kind'), 'agent': s.get('agent'),
            'role': 'overseer' if _is_overseer_session(s, ctx) else s.get('role'), 'status': s.get('status'),
            'observed_at': store.iso(), 'cwd': s.get('cwd'), 'host_ref': s.get('host_ref') or 'local',
            'wired': _wired(policy, s) and not _is_overseer_session(s, ctx), 'active_task_id': ext.get('active_task_id'),
            'last_message_head': None, 'retired': bool(ext.get('retired')),
        })
    ws = store.workspace
    return {
        'overseer_workspace': {'id': ws.get('id'), 'name': ws.get('title'), 'paused': bool(policy.get('paused')),
                               'cycle': store.get_extension('cycle_count') or 0, 'transport': getattr(transport, 'name', '?')},
        'workspaces': [{'id': ws.get('id'), 'name': ws.get('title'), 'own': True, 'sessions': out}],
    }


def h_read(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    session = resolve_session(ctx, a.get('session'))
    mode = a.get('mode') or 'last_message'
    text = _read_session(ctx, session, mode, int(a.get('lines') or 2000))
    digest = sha256_hex(text)
    captured_at = store.iso()
    out: dict[str, Any] = {'session': session['id'], 'callsign': session.get('callsign'), 'status': session.get('status'), 'mode': mode,
                           'source': 'capture', 'captured_at': captured_at, 'digest': digest, 'text': text}
    if a.get('keep') and text:
        art = store.put_artifact(kind='log', title=f"{session.get('callsign')} {mode} {captured_at}", text=text,
                                 produced_by=actor_for(ctx), session_id=session['id'],
                                 metadata={'mode': mode, 'source': 'capture', 'provenance': [
                                     {'kind': 'session', 'ref_id': session['id'], 'captured_at': captured_at,
                                      'digest': {'algorithm': 'sha256', 'value': digest}}]})
        out['artifact_id'] = art['id']
    return out


def h_work_create(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    if not a.get('title'):
        raise OverseerError('title is required')
    criteria = []
    for i, c in enumerate(a.get('acceptance_criteria') or []):
        criteria.append({
            'id': f"criterion.{slugify(str(c.get('statement') or 'c'))[:30] or 'c'}.{i + 1}.{_short()}", 'statement': c.get('statement'),
            'required': c.get('required', True) is not False,
            'verifier': {'kind': (c.get('verifier') or {}).get('kind') or 'inspection', 'spec': (c.get('verifier') or {}).get('spec') or {}},
            'status': 'pending', 'evidence_artifact_ids': [],
        })
    fields: dict[str, Any] = {
        'id': f"work.{slugify(str(a['title']))[:40] or 'item'}.{_short()}", 'kind': a.get('kind') or 'task', 'title': a['title'],
        'description': a.get('description') or '', 'status': 'backlog', 'priority': a.get('priority') or 'normal',
        'owner': actor_for(ctx), 'scopes': _norm_scopes(a.get('scopes')), 'acceptance_criteria': criteria,
        'extensions': {'owner_role': a.get('owner_role')},
    }
    if a.get('parent_id'):
        fields['parent_id'] = a['parent_id']
    return store.create('work_item', fields, actor=actor_for(ctx))


def h_work_update(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    wi = store.get('work_item', a.get('id') or '')
    if wi is None:
        raise OverseerError(f"no work item {a.get('id')}")
    p = a.get('patch') or {}
    patch: dict[str, Any] = {}
    for f in ('title', 'description', 'priority', 'parent_id'):
        if f in p:
            patch[f] = p[f]
    if 'scopes' in p:
        patch['scopes'] = _norm_scopes(p['scopes'])
    if 'acceptance_criteria' in p:
        merged = []
        for i, c in enumerate(p['acceptance_criteria'] or []):
            prev = next((x for x in wi['acceptance_criteria'] if x.get('id') == c.get('id')), None)
            if prev:
                merged.append({**prev, 'statement': c.get('statement', prev['statement']), 'required': c.get('required', prev['required']),
                               'verifier': c.get('verifier') or prev['verifier']})
            else:
                merged.append({'id': f"criterion.{slugify(str(c.get('statement') or 'c'))[:30] or 'c'}.{i + 1}.{_short()}",
                               'statement': c.get('statement'), 'required': c.get('required', True) is not False,
                               'verifier': {'kind': (c.get('verifier') or {}).get('kind') or 'inspection', 'spec': (c.get('verifier') or {}).get('spec') or {}},
                               'status': 'pending', 'evidence_artifact_ids': []})
        patch['acceptance_criteria'] = merged
    if 'owner_role' in p:
        patch['extensions'] = {'owner_role': p['owner_role']}
    return store.update('work_item', wi['id'], int(a['expected_revision']), patch, actor=actor_for(ctx))


def h_work_transition(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    return store.transition('work_item', a.get('id') or '', int(a['expected_revision']), a.get('event') or '', actor=actor_for(ctx),
                            reason=a.get('reason'))


def h_work_list(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    items = _flat_items(store)
    if a.get('id'):
        items = [i for i in items if i['id'] == a['id']]
    if a.get('status'):
        items = [i for i in items if i.get('status') == a['status']]
    if a.get('owner_role'):
        items = [i for i in items if str(i.get('owner_role') or '').lower() == str(a['owner_role']).lower()]
    return {'count': len(items), 'items': items}


def h_dispatch(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    sessions = transport_for(ctx).list_sessions()
    session = resolve_session(ctx, a.get('session'), sessions)
    check_send_policy(ctx, 'dispatch', session, content=str(a.get('prompt') or ''), force=bool(a.get('force')))
    wi = store.get('work_item', a.get('work_item_id') or '')
    if wi is None:
        raise OverseerError(f"no work item {a.get('work_item_id')}")
    if wi.get('status') in ('done', 'cancelled'):
        raise OverseerError(f"work item {wi['id']} is {wi['status']}")
    sync_sessions(store, sessions, ctx)
    sid = session['id']
    sess = store.get('session', sid) or {}
    active = (sess.get('extensions') or {}).get('active_task_id')
    if active:
        t = store.get('task', active)
        if t and t.get('status') not in ('succeeded', 'failed', 'cancelled'):
            raise OverseerError(f"{session.get('callsign')} already has active task {t['id']} ({t['status']}) — checkpoint it first")
    actor = actor_for(ctx)
    runs = store.list('run', lambda r: wi['id'] in (r.get('work_item_ids') or []) and r.get('status') not in ('completed', 'failed', 'cancelled'))
    run = runs[0] if runs else store.create('run', {'id': f'run.{_short()}', 'work_item_ids': [wi['id']], 'task_ids': [],
                                                    'initiated_by': actor}, actor=actor)
    if not runs:
        store.update('work_item', wi['id'], wi['revision'], {'run_ids': list(wi.get('run_ids') or []) + [run['id']]}, actor=SYSTEM)
    attempt = len(store.list('task', lambda t: t.get('work_item_id') == wi['id'])) + 1
    task_id = f'task.{_short()}'
    prompt = str(a.get('prompt') or '')
    if a.get('isolation') == 'worktree':
        prompt = (f'Work in an isolated git worktree: create one from the current HEAD (e.g. `git worktree add ../<repo>-{task_id[5:]} '
                  f'-b {task_id[5:]}`), do all edits there, and report the branch name and worktree path when done. '
                  f'Do not modify the main working tree.\n\n{prompt}')
    header = f"[overseer → {task_id} · {wi['title']}]"
    message = f'{header}\n{prompt}'
    prompt_art = store.put_artifact(kind='context', title=f'prompt {task_id}', text=message, produced_by=actor, session_id=sid,
                                    metadata={'task_id': task_id, 'work_item_id': wi['id']})
    scopes = _norm_scopes(a.get('scopes'))
    store.create('task', {
        'id': task_id, 'work_item_id': wi['id'], 'run_id': run['id'], 'title': a.get('title') or f"{wi['title']} → {session.get('callsign')}",
        'instruction': message, 'status': 'claimed', 'owner': {'kind': 'agent', 'id': f"agent.{session.get('block_id') or session.get('name') or sid[8:]}"},
        'attempt': attempt, 'max_attempts': 3, 'scopes': scopes, 'session_id': sid, 'idempotency_key': f"{wi['id']}:{sid}:{attempt}",
        'extensions': {'isolation': a.get('isolation') or 'none', 'prompt_artifact_id': prompt_art['id']},
    }, actor=actor)
    manifest = store.create('context_manifest', {
        'id': f'manifest.{_short()}', 'task_id': task_id, 'compiler': {'name': 'charon-overseer', 'version': VERSION},
        'query': {'work_item_id': wi['id'], 'session_id': sid}, 'budget': {'unit': 'byte', 'limit': 65536, 'used': len(message)},
        'entries': [{'record_ref': {'record_type': 'artifact', 'id': prompt_art['id']}, 'section': 'prompt', 'order': 0, 'score': 1,
                     'score_components': {}, 'reasons': ['dispatched prompt'], 'representation': 'inline',
                     'digest': prompt_art['digest'], 'units': len(message)}],
        'exclusions': [], 'digest': prompt_art['digest'],
        **({'successor_of_manifest_id': a['reuse_manifest_id']} if a.get('reuse_manifest_id') and store.get('context_manifest', a['reuse_manifest_id']) else {}),
    }, actor=actor)
    store.update('task', task_id, store.get('task', task_id)['revision'], {'context_manifest_id': manifest['id']}, actor=SYSTEM)
    write_scopes = [s for s in scopes if s.get('access') != 'read']
    leases: list[dict[str, Any]] = []
    forced_over: list[dict[str, Any]] = []
    if write_scopes:
        try:
            leases = store.lease_acquire(task_id=task_id, session_id=sid, scopes=write_scopes, mode=a.get('lease') or 'exclusive',
                                         ttl_ms=LEASE_TTL_MS, actor=actor)
        except LeaseConflict as exc:
            if not a.get('force'):
                t = store.get('task', task_id)
                store.transition('task', task_id, t['revision'], 'cancel', actor=SYSTEM, reason='lease conflict')
                names = []
                for c in exc.conflicts:
                    owner = c['lease'].get('owner_session_id')
                    rec = store.get('session', owner) if owner else None
                    names.append(((rec or {}).get('extensions') or {}).get('callsign') or owner or c['lease'].get('owner_task_id') or '?')
                selectors = sorted({c['scope']['selector'] for c in exc.conflicts})
                raise OverseerError(f"CONFLICT: {exc} — scopes {', '.join(selectors)} are held by {', '.join(dict.fromkeys(names))}; "
                                    f"serialize or split the item (do not force)") from exc
            forced_over = [{'scope': c['scope'], 'lease_id': c['lease']['id']} for c in exc.conflicts]
    via = _send(ctx, session, message)
    t = store.get('task', task_id)
    store.transition('task', task_id, t['revision'], 'start', actor=SYSTEM)
    s2 = store.get('session', sid)
    if s2 is not None:
        store.update('session', sid, s2['revision'], {'task_ids': list(s2.get('task_ids') or []) + [task_id]}, actor=SYSTEM)
        store.set_session_status(sid, store.get('session', sid)['status'], actor=SYSTEM, extensions={'active_task_id': task_id})
    wi2 = store.get('work_item', wi['id'])
    if wi2.get('status') == 'ready':
        try:
            store.transition('work_item', wi2['id'], wi2['revision'], 'start', actor=SYSTEM, reason=f'dispatched {task_id}')
        except (IllegalTransition, RevisionConflict, GuardFailed):
            pass
    r2 = store.get('run', run['id'])
    store.update('run', run['id'], r2['revision'], {'task_ids': list(r2.get('task_ids') or []) + [task_id]}, actor=SYSTEM)
    store.append_event('agent_intervention', actor=actor, correlation_id=sid,
                       subject_refs=[{'record_type': 'task', 'id': task_id}, {'record_type': 'session', 'id': sid}],
                       payload={'session_id': sid, 'task_id': task_id, 'work_item_id': wi['id'], 'content_digest': prompt_art['digest']['value'],
                                'content_head': message[:200], 'via': via, 'branch_label': 'dispatch', 'forced': bool(a.get('force')),
                                **({'forced_over': forced_over} if forced_over else {})})
    return {'task_id': task_id, 'run_id': run['id'], 'manifest_id': manifest['id'], 'lease_ids': [lease['id'] for lease in leases],
            'attempt': attempt, 'sent_via': via, 'session': sid}


def h_intervene(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    session = resolve_session(ctx, a.get('session'))
    content = str(a.get('content') or '')
    check_send_policy(ctx, 'intervene', session, content=content, force=bool(a.get('force')))
    sid = session['id']
    target = None
    try:
        last = _read_session(ctx, session, 'last_message')
        if last:
            target = {'digest': sha256_hex(last), 'head': last[:160]}
    except Exception:
        target = None
    via = _send(ctx, session, content)
    ev = store.append_event('agent_intervention', actor=actor_for(ctx), correlation_id=sid,
                            subject_refs=[{'record_type': 'session', 'id': sid}],
                            payload={'session_id': sid, 'content_digest': sha256_hex(content), 'content_head': content[:200],
                                     'intervention_of': target, 'branch_label': a.get('branch_label'), 'via': via, 'forced': bool(a.get('force'))})
    return {'session': sid, 'sent_via': via, 'event_id': ev['id'], 'intervention_of': target}


def h_interrupt(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    session = resolve_session(ctx, a.get('session'))
    check_send_policy(ctx, 'interrupt', session)
    key = 'esc' if a.get('key') == 'esc' else 'ctrl-c'
    try:
        transport_for(ctx).interrupt(session['id'], key)
    except Exception as exc:
        raise OverseerError(f"cannot interrupt {session.get('callsign')}: {exc}") from exc
    store.append_event('agent_intervention', actor=actor_for(ctx), subject_refs=[{'record_type': 'session', 'id': session['id']}],
                       payload={'session_id': session['id'], 'interrupt': key})
    return {'ok': True, 'session': session['id'], 'key': key}


def h_checkpoint(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    t = store.get('task', a.get('task_id') or '')
    if t is None:
        raise OverseerError(f"no task {a.get('task_id')}")
    if t.get('status') in ('succeeded', 'failed', 'cancelled'):
        raise OverseerError(f"task {t['id']} is already {t['status']}")
    ext = t.get('extensions') or {}
    if ext.get('isolation') == 'worktree' and not a.get('branch'):
        raise OverseerError('branch is required for a worktree-isolated task')
    actor = actor_for(ctx)
    result = {'changed_paths': list(a.get('changed_paths') or []), 'branch': a.get('branch'), 'validation': list(a.get('validation') or []),
              'risks': list(a.get('risks') or [])}
    cur = store.update('task', t['id'], t['revision'], {'result_summary': a.get('summary'), 'completed_at': store.iso(),
                                                       'extensions': {'result': result}}, actor=actor)
    cur = store.transition('task', t['id'], cur['revision'], 'fail' if a.get('result') == 'failed' else 'succeed', actor=actor,
                           reason=a.get('summary'))
    for lid in cur.get('lease_ids') or []:
        lease = store.get('lease', lid)
        if lease and lease.get('status') == 'active':
            store.lease_release(lid, 'checkpoint', actor=SYSTEM)
    s = store.get('session', t.get('session_id') or '')
    if s is not None and (s.get('extensions') or {}).get('active_task_id') == t['id']:
        store.set_session_status(s['id'], s['status'], actor=SYSTEM, extensions={'active_task_id': None})
    store.append_event('agent_message', actor={'kind': 'agent', 'id': f"agent.{str(t.get('session_id') or '')[8:] or 'session'}"},
                       subject_refs=[{'record_type': 'task', 'id': t['id']}, {'record_type': 'work_item', 'id': t['work_item_id']}],
                       payload={'summary': a.get('summary'), 'result': a.get('result'), 'changed_paths': result['changed_paths'], 'branch': result['branch']})
    return {'task_id': t['id'], 'status': cur['status'], 'work_item_id': t['work_item_id'],
            'note': 'work item status unchanged — attach evidence, then submit/pass'}


def h_evidence_attach(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    wi = store.get('work_item', a.get('work_item_id') or '')
    if wi is None:
        raise OverseerError(f"no work item {a.get('work_item_id')}")
    crit = next((c for c in wi.get('acceptance_criteria') or [] if c.get('id') == a.get('criterion_id')), None)
    if crit is None:
        raise OverseerError(f"no criterion {a.get('criterion_id')} on {wi['id']}")
    task = store.get('task', a.get('task_id') or '')
    if task is None:
        raise OverseerError(f"no task {a.get('task_id')}")
    actor = actor_for(ctx)
    if (crit.get('verifier') or {}).get('kind') == 'approval':
        gate = store.create_gate(kind='approval', question=f"Approve: {crit.get('statement')}", options=['Approve', 'Reject'],
                                 refs=[wi['id'], crit['id'], task['id']], actor=actor,
                                 related={'work_item_id': wi['id'], 'criterion_id': crit['id'], 'task_id': task['id'], 'summary': a.get('summary') or ''})
        return {'pending': True, 'gate_id': gate['id'], 'note': 'approval criteria are decided by the user in the Gates inbox'}
    art_id = a.get('artifact_id')
    passed = bool(a.get('passed'))
    if not art_id:
        vk = (crit.get('verifier') or {}).get('kind')
        kind = 'test_result' if vk in ('command', 'test') else ('metric' if vk == 'metric' else 'inspection')
        parts = [f"$ {a['command']}" if a.get('command') else None, f"exit {a['exit_code']}" if a.get('exit_code') is not None else None,
                 a.get('summary') or None, f"---\n{redact(a['output'])}" if a.get('output') else None]
        body = '\n'.join(p for p in parts if p) or f'(no output) passed={passed}'
        art = store.put_artifact(kind=kind, title=f"evidence {crit['id']} ({'passed' if passed else 'failed'})", text=body, produced_by=actor,
                                 task_id=task['id'], session_id=task.get('session_id'),
                                 metadata={'criterion_id': crit['id'], 'work_item_id': wi['id'], 'passed': passed, 'command': a.get('command'),
                                           'exit_code': a.get('exit_code')})
        art_id = art['id']
    elif store.get('artifact', art_id) is None:
        raise OverseerError(f'no artifact {art_id}')
    store.attach_evidence(wi['id'], wi['revision'], crit['id'], status='passed' if passed else 'failed', artifact_ids=[art_id],
                          task_id=task['id'], actor=actor)
    wi2 = store.get('work_item', wi['id'])
    unmet = [u.split(' ', 1)[0] for u in unmet_required_criteria(wi2)]
    return {'criterion_id': crit['id'], 'status': 'passed' if passed else 'failed', 'artifact_id': art_id,
            'work_item_revision': wi2['revision'], 'unmet_required': unmet}


def h_lease_heartbeat(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    lease = store.lease_heartbeat(a.get('lease_id') or '')
    return {'lease_id': lease['id'], 'expires_at': lease['expires_at']}


def h_lease_release(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    lease = store.lease_release(a.get('lease_id') or '', a.get('reason') or 'released by overseer', actor=actor_for(ctx))
    return {'lease_id': lease['id'], 'status': lease['status']}


def h_spawn(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    policy = policy_for(ctx)
    if policy.get('paused'):
        raise OverseerError('policy.paused: overseer is PAUSED by the user')
    transport = transport_for(ctx)
    sessions = transport.list_sessions()
    v = evaluate_spawn_policy(policy=str(policy.get('spawn') or 'allow'), session_count=len(sessions), max_blocks=int(policy.get('max_blocks') or 8))
    if not v.get('ok'):
        raise OverseerError(f"policy.{v['code']}: {v['reason']}")
    role = str(a.get('role') or 'Agent').strip()
    agent = 'codex' if a.get('agent') == 'codex' else 'claude'
    if v.get('confirm'):
        gate = store.create_gate(kind='spawn', question=f'Spawn a {role} ({agent})?', options=['Spawn', 'Decline'], actor=actor_for(ctx),
                                 related={'action': {'tool': 'acheron_spawn', 'args': dict(a)}, 'prompt_head': str(a.get('prompt') or '')[:200]})
        return {'pending': True, 'gate_id': gate['id']}
    try:
        s = transport.spawn(role, agent, a.get('cwd'), str(a.get('prompt') or ''))
    except NotImplementedError as exc:
        raise OverseerError(f'spawn is not available on this transport: {exc}') from exc
    except Exception as exc:
        raise OverseerError(f'spawn failed: {exc}') from exc
    sync_sessions(store, [s], ctx)
    store.append_event('session.spawned', actor=actor_for(ctx), subject_refs=[{'record_type': 'session', 'id': s['id']}],
                       payload={'role': role, 'agent': agent, 'cwd': a.get('cwd'), 'prompt_head': str(a.get('prompt') or '')[:200]})
    if a.get('prompt'):
        try:
            transport.send(s['id'], str(a['prompt']))
        except Exception:
            pass
    return {'session': s['id'], 'block_id': s.get('block_id'), 'callsign': s.get('callsign') or role, 'agent': agent,
            'cwd': a.get('cwd'), 'wired': True}


def h_propose(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    actor = actor_for(ctx)
    prop = store.create_proposal(kind=a.get('kind') or 'knowledge', title=a.get('title') or '', rationale=a.get('rationale') or '',
                                 alternatives=a.get('alternatives') or [], action=a.get('action'), refs=a.get('refs') or [], actor=actor)
    gate = store.create_gate(kind='proposal', question=f"{a.get('kind')}: {a.get('title')}", options=['Accept', 'Reject'],
                             refs=[prop['id'], *(a.get('refs') or [])], actor=actor, related={'proposal_id': prop['id']})
    return {'proposal_id': prop['id'], 'gate_id': gate['id'], 'status': 'proposed'}


def h_decide(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    actor = actor_for(ctx)
    alts = list(a.get('alternatives') or [])
    body = str(a.get('why') or '')
    if alts:
        body += '\n\nAlternatives considered:\n' + '\n'.join(f'- {x}' for x in alts)
    rec = store.create('knowledge_item', {'id': f'knowledge.decision.{_short()}', 'kind': 'decision', 'title': str(a.get('what') or '')[:500],
                                          'body': body, 'status': 'proposed', 'confidence': 0.8,
                                          'extensions': {'topic': a.get('topic'), 'alternatives': alts, 'refs': a.get('refs') or []}}, actor=actor)
    store.append_event('decision', actor=actor, subject_refs=[{'record_type': 'knowledge_item', 'id': rec['id']}],
                       payload={'what': a.get('what'), 'why': a.get('why'), 'topic': a.get('topic'), 'alternatives': alts, 'refs': a.get('refs') or []})
    return {'knowledge_item_id': rec['id'], 'status': rec['status']}


def h_label(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    session = resolve_session(ctx, a.get('session'))
    check_send_policy(ctx, 'label', session)
    text = str(a.get('text') or '').strip()
    transport = transport_for(ctx)
    if hasattr(transport, 'label'):
        try:
            transport.label(session['id'], text)
        except Exception as exc:
            raise OverseerError(f'cannot label {session.get("callsign")}: {exc}') from exc
    rec = store.get('session', session['id'])
    if rec is not None:
        store.set_session_status(session['id'], rec['status'], actor=SYSTEM, extensions={'callsign': text or session.get('callsign'), 'role': text or None})
    return {'session': session['id'], 'callsign': text or session.get('callsign')}


def h_report(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    cycle = int(store.get_extension('cycle_count') or 0)
    report = {'summary': a.get('summary'), 'health': a.get('health'), 'staffing': a.get('staffing') or '',
              'work_division': a.get('work_division') or '', 'risks': list(a.get('risks') or []), 'next': a.get('next') or '',
              'at': store.iso(), 'cycle': cycle}
    store.set_extension('report', report)
    store.append_event('cycle.report', actor=actor_for(ctx), payload={'cycle': cycle, **report})
    (store.root / 'overseer.json').write_text(json.dumps({'report': report, 'cycle': cycle, 'generated_at': store.iso()}, indent=2), encoding='utf-8')
    store.write_projections(status=True)
    _MUTATIONS[str(store.root)] = []
    return {'ok': True, 'cycle': cycle}


def h_ask_user(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    open_q = store.list('gate', lambda g: g.get('status') == 'open' and g.get('kind') == 'question')
    if len(open_q) >= MAX_OPEN_QUESTIONS:
        raise OverseerError(f'{MAX_OPEN_QUESTIONS} questions are already open — wait for answers')
    gate = store.create_gate(kind='question', question=str(a.get('question') or ''), options=a.get('options') or [], refs=a.get('refs') or [],
                             actor=actor_for(ctx))
    return {'queued': True, 'id': gate['id']}


def h_wait(ctx: ToolContext, store: WorkspaceStore, a: dict[str, Any]) -> dict[str, Any]:
    secs = max(1, min(120, int(a.get('seconds') or 60)))
    baseline = len(store.events)
    deadline = time.time() + secs
    while time.time() < deadline:
        fresh = WorkspaceStore.open(store.root, workspace_id=store.workspace_id, replica_id=store.replica_id)
        if len(fresh.events) > baseline:
            new = fresh.events[baseline:]
            return {'events': [f"{e.get('event_type')} {json.dumps(e.get('payload') or {}, ensure_ascii=False)[:160]}" for e in new]}
        time.sleep(0.5)
    return {'events': []}


HANDLERS: dict[str, Callable[[ToolContext, WorkspaceStore, dict[str, Any]], dict[str, Any]]] = {
    'acheron_fleet': h_fleet, 'acheron_read': h_read, 'acheron_work_create': h_work_create, 'acheron_work_update': h_work_update,
    'acheron_work_transition': h_work_transition, 'acheron_work_list': h_work_list, 'acheron_dispatch': h_dispatch,
    'acheron_intervene': h_intervene, 'acheron_interrupt': h_interrupt, 'acheron_checkpoint': h_checkpoint,
    'acheron_evidence_attach': h_evidence_attach, 'acheron_lease_heartbeat': h_lease_heartbeat, 'acheron_lease_release': h_lease_release,
    'acheron_spawn': h_spawn, 'acheron_propose': h_propose, 'acheron_decide': h_decide, 'acheron_label': h_label,
    'acheron_report': h_report, 'acheron_ask_user': h_ask_user, 'acheron_wait': h_wait,
}


# ── executor ───────────────────────────────────────────────────────────────

def execute_overseer(mcp_name: str, params: dict, ctx: ToolContext) -> ToolResult:
    handler = HANDLERS.get(mcp_name)
    if handler is None:
        return ToolResult(content=f'unknown overseer tool {mcp_name}', is_error=True)
    t0 = time.time()
    try:
        store = open_store(ctx)
        if mcp_name in MUTATING:
            _rate_limit(store)
        result = handler(ctx, store, dict(params or {}))
        ok, error = True, None
    except (OverseerError, LeaseConflict, GuardFailed, IllegalTransition, RevisionConflict, NotFound, ValidationError) as exc:
        result, ok, error = None, False, str(exc)
        if isinstance(exc, LeaseConflict):
            error = f'CONFLICT: {exc}'
        elif isinstance(exc, GuardFailed):
            error = f'{exc} (unmet: {", ".join(getattr(exc, "unmet", []) or [])})'
    except Exception as exc:  # never crash the caller's loop
        result, ok, error = None, False, f'{type(exc).__name__}: {exc}'
    try:
        root = workspace_root(ctx)
        root.mkdir(parents=True, exist_ok=True)
        with open(root / 'journal.jsonl', 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'name': mcp_name, 'args': _truncate(params),
                                 'ok': ok, 'ms': int((time.time() - t0) * 1000), 'surface': _meta(ctx).get('surface') or 'charon',
                                 **({'error': error} if error else {})}, ensure_ascii=False) + '\n')
    except OSError:
        pass
    if not ok:
        return ToolResult(content=str(error), is_error=True)
    return ToolResult(content=json.dumps(result, ensure_ascii=False, default=str), details=result)


def _truncate(a: Any) -> Any:
    if not isinstance(a, dict):
        return a
    return {k: (v[:400] + f'…(+{len(v) - 400})' if isinstance(v, str) and len(v) > 400 else v) for k, v in a.items()}


OVERSEER_TOOL_EXECUTORS: dict[str, Callable[[dict, ToolContext], ToolResult]] = {
    charon_name: partial(execute_overseer, mcp_name) for mcp_name, charon_name in MCP_TO_CHARON.items()
}
