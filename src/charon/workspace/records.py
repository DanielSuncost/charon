"""Record shapes, defaults, ids, canonical JSON and errors for ``charon.workspace``.

Mirrors Acheron's ``src/overseer/kernel.js`` field lists and defaults exactly so the
two implementations are interchangeable (same bundle, same event chain).  Every record
carries ONLY Workspace-contract fields (``docs/contracts/workspace.schema.json``);
anything else a caller passes is moved into ``extensions``.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable


# ── errors ──────────────────────────────────────────────────────────────────

class WorkspaceError(RuntimeError):
    """Base class for workspace errors."""

    def __init__(self, message: str, **extra: Any):
        super().__init__(message)
        for key, value in extra.items():
            setattr(self, key, value)


class RevisionConflict(WorkspaceError):
    """expected_revision does not match the stored record."""


class IllegalTransition(WorkspaceError):
    """The event is not legal from the record's current state (or the state is terminal)."""


class GuardFailed(WorkspaceError):
    """A guarded transition (e.g. work_item ``pass``) rejected the event."""

    def __init__(self, message: str, unmet: list[str] | None = None, **extra: Any):
        super().__init__(message, unmet=list(unmet or []), **extra)


class LeaseConflict(WorkspaceError):
    """Requested scopes overlap active leases."""

    def __init__(self, message: str, conflicts: list[dict[str, Any]] | None = None):
        super().__init__(message, conflicts=list(conflicts or []))


class EventCollision(WorkspaceError):
    """An event id was reused with different content."""


class NotFound(WorkspaceError):
    """The record does not exist."""


class ValidationError(WorkspaceError):
    """A record or argument is malformed."""


# ── canonical JSON / hashing / ids / time ───────────────────────────────────

def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, UTF-8 kept as-is — byte-identical to the JS kernel's
    ``canonicalJSON`` for the value types the contract uses."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def sha256_hex(value: str | bytes) -> str:
    data = value.encode('utf-8') if isinstance(value, str) else bytes(value)
    return hashlib.sha256(data).hexdigest()


def digest_of(text: str) -> dict[str, str]:
    return {'algorithm': 'sha256', 'value': sha256_hex(text)}


def iso_from_ms(ms: float) -> str:
    """RFC3339 UTC with milliseconds and ``Z`` — the JS ``Date#toISOString`` format."""
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


def parse_iso_ms(value: str | None) -> float | None:
    if not value or not isinstance(value, str):
        return None
    try:
        text = value.replace('Z', '+00:00')
        return datetime.fromisoformat(text).timestamp() * 1000.0
    except ValueError:
        return None


def now_ms() -> float:
    return datetime.now(timezone.utc).timestamp() * 1000.0


def new_uuid() -> str:
    return str(uuid.uuid4())


ID_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_.:-]{0,127}$')
SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,62}$')
EVENT_TYPE_RE = re.compile(r'^[a-z][a-z0-9_.:-]{0,127}$')


def assert_id(value: Any, what: str = 'id') -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValidationError(f'{what} "{value}" is not a valid identifier')
    return value


def slugify(text: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', str(text or '').lower()).strip('-')
    return slug or 'item'


def clone(value: Any) -> Any:
    return json.loads(json.dumps(value)) if value is not None else None


# ── constants (identical to kernel.js) ─────────────────────────────────────

SCHEMA_VERSION = '1.0'
KERNEL_VERSION = '1'
DEFAULT_LEASE_TTL_MS = 30 * 60 * 1000
SYSTEM_ACTOR = {'kind': 'system', 'id': 'system.charon'}
USER_ACTOR = {'kind': 'user', 'id': 'user.local'}

RECORD_TYPES = ['work_item', 'run', 'task', 'lease', 'session', 'artifact', 'context_manifest',
                'policy', 'knowledge_item', 'replica', 'proposal', 'gate']
SCHEMA_TYPES = frozenset(['work_item', 'run', 'task', 'lease', 'session', 'artifact', 'context_manifest',
                          'policy', 'knowledge_item', 'replica'])
BUNDLE_KEY = {'work_item': 'work_items', 'run': 'runs', 'task': 'tasks', 'lease': 'leases', 'session': 'sessions',
              'artifact': 'artifacts', 'context_manifest': 'context_manifests', 'policy': 'policies',
              'knowledge_item': 'knowledge_items', 'replica': 'replicas'}
ID_PREFIX = {'work_item': 'work', 'run': 'run', 'task': 'task', 'lease': 'lease', 'session': 'session',
             'artifact': 'artifact', 'context_manifest': 'manifest', 'policy': 'policy', 'knowledge_item': 'knowledge',
             'replica': 'replica', 'proposal': 'proposal', 'gate': 'gate'}

BASE_FIELDS = ['record_type', 'id', 'workspace_id', 'revision', 'created_at', 'updated_at', 'labels', 'provenance', 'extensions']
TYPE_FIELDS: dict[str, list[str]] = {
    'work_item': ['kind', 'title', 'description', 'status', 'priority', 'parent_id', 'dependency_ids', 'blocked_by_ids', 'owner',
                  'assignees', 'scopes', 'acceptance_criteria', 'run_ids', 'due_at', 'terminal_reason'],
    'run': ['workflow_id', 'workflow_revision', 'status', 'work_item_ids', 'task_ids', 'initiated_by', 'started_at', 'completed_at',
            'terminal_reason', 'event_cursors', 'metrics'],
    'task': ['work_item_id', 'run_id', 'title', 'instruction', 'status', 'owner', 'attempt', 'max_attempts', 'scopes',
             'dependency_task_ids', 'context_manifest_id', 'lease_ids', 'session_id', 'started_at', 'completed_at',
             'result_summary', 'evidence_artifact_ids', 'idempotency_key'],
    'lease': ['resource', 'mode', 'owner_task_id', 'owner_session_id', 'fencing_token', 'status', 'acquired_at', 'heartbeat_at',
              'expires_at', 'released_at', 'previous_lease_id', 'release_reason'],
    'session': ['kind', 'status', 'actor', 'replica_id', 'host_ref', 'task_ids', 'parent_session_id', 'continuation_of_session_id',
                'started_at', 'ended_at', 'checkpoint_artifact_id', 'transcript_artifact_id', 'event_cursors'],
    'artifact': ['kind', 'title', 'media_type', 'locator', 'digest', 'size_bytes', 'immutable', 'status', 'produced_by', 'task_id',
                 'run_id', 'session_id', 'source_revision', 'metadata'],
    'context_manifest': ['task_id', 'compiler', 'workspace_revision', 'event_cursors', 'built_at', 'query', 'budget', 'policy_ids',
                         'entries', 'exclusions', 'digest', 'successor_of_manifest_id'],
    'policy': ['name', 'version', 'status', 'priority', 'effect', 'actions', 'subject_selectors', 'resource_selectors', 'conditions',
               'rationale'],
    'knowledge_item': ['kind', 'title', 'body', 'status', 'confidence', 'scopes', 'observation_ids', 'evidence_artifact_ids',
                       'contradicts_ids', 'supersedes_ids', 'valid_from', 'valid_until', 'review', 'tags'],
    'replica': ['name', 'kind', 'trust', 'identity', 'cursors', 'last_seen_at', 'capabilities', 'policy_ids'],
    'workspace': ['slug', 'title', 'description', 'state', 'roots', 'home_replica_id', 'default_policy_ids'],
    'proposal': ['kind', 'title', 'rationale', 'alternatives', 'action', 'refs', 'status', 'proposed_by', 'decided_by', 'decided_at',
                 'decision_reason'],
    'gate': ['kind', 'question', 'options', 'refs', 'status', 'opened_by', 'answer', 'decided_by', 'decided_at', 'related'],
}

# Lifecycle tables (state → event → next); the schema enums are the states.
FSM: dict[str, dict[str, dict[str, str]]] = {
    'work_item': {
        'backlog': {'ready': 'ready', 'cancel': 'cancelled'},
        'ready': {'start': 'active', 'block': 'blocked', 'cancel': 'cancelled'},
        'active': {'submit': 'review', 'block': 'blocked', 'cancel': 'cancelled'},
        'blocked': {'unblock': 'active', 'cancel': 'cancelled'},
        'review': {'pass': 'done', 'fail': 'active', 'cancel': 'cancelled'},
        'done': {}, 'cancelled': {},
    },
    'task': {
        'pending': {'claim': 'claimed', 'cancel': 'cancelled'},
        'claimed': {'start': 'running', 'succeed': 'succeeded', 'fail': 'failed', 'cancel': 'cancelled'},
        'running': {'wait': 'waiting', 'block': 'blocked', 'succeed': 'succeeded', 'fail': 'failed', 'cancel': 'cancelled'},
        'waiting': {'resume': 'running', 'succeed': 'succeeded', 'fail': 'failed', 'cancel': 'cancelled'},
        'blocked': {'unblock': 'running', 'fail': 'failed', 'cancel': 'cancelled'},
        'succeeded': {}, 'failed': {}, 'cancelled': {},
    },
    'lease': {
        'requested': {'activate': 'active', 'revoke': 'revoked'},
        'active': {'release': 'released', 'expire': 'expired', 'revoke': 'revoked'},
        'released': {}, 'expired': {}, 'revoked': {},
    },
    'knowledge_item': {
        'proposed': {'verify': 'verified', 'contest': 'contested', 'reject': 'rejected', 'retract': 'retracted'},
        'verified': {'contest': 'contested', 'retract': 'retracted', 'supersede': 'superseded', 'expire': 'expired', 'stale': 'stale'},
        'contested': {'verify': 'verified', 'reject': 'rejected', 'retract': 'retracted'},
        'stale': {'verify': 'verified', 'retract': 'retracted', 'expire': 'expired'},
        'rejected': {}, 'retracted': {}, 'superseded': {}, 'expired': {},
    },
    'proposal': {
        'proposed': {'accept': 'accepted', 'reject': 'rejected', 'withdraw': 'withdrawn'},
        'accepted': {'apply': 'applied', 'withdraw': 'withdrawn'},
        'applied': {}, 'rejected': {}, 'withdrawn': {},
    },
    'gate': {
        'open': {'answer': 'answered', 'resolve': 'resolved'},
        'answered': {'resolve': 'resolved'},
        'resolved': {},
    },
}
SESSION_STATUSES = ['starting', 'active', 'idle', 'disconnected', 'completed', 'failed', 'stopped']
WORK_ITEM_KINDS = ['objective', 'initiative', 'task', 'defect', 'question', 'decision', 'review']
PRIORITIES = ['low', 'normal', 'high', 'urgent']
SESSION_KINDS = ['interactive', 'worker', 'coordinator', 'remote']
PROPOSAL_KINDS = ['staffing', 'scope', 'priority', 'retire', 'knowledge', 'procedure']
GATE_KINDS = ['question', 'approval', 'spawn', 'proposal', 'conflict']


# ── normalizers ─────────────────────────────────────────────────────────────

def normalize_scope(scope: Any) -> dict[str, Any]:
    if not isinstance(scope, dict):
        raise ValidationError('scope must be an object')
    if not scope.get('selector'):
        raise ValidationError('scope.selector is required')
    out: dict[str, Any] = {'kind': scope.get('kind') or 'path', 'selector': str(scope['selector']), 'access': scope.get('access') or 'write'}
    if scope.get('recursive'):
        out['recursive'] = True
    exclusions = scope.get('exclusions')
    if isinstance(exclusions, list) and exclusions:
        out['exclusions'] = list(exclusions)
    return out


def normalize_criteria(incoming: Iterable[Any] | None, existing: Iterable[dict[str, Any]] | None, new_id: Callable[[], str]) -> list[dict[str, Any]]:
    by_id = {c.get('id'): c for c in (existing or [])}
    out: list[dict[str, Any]] = []
    for c in incoming or []:
        if not isinstance(c, dict):
            raise ValidationError('acceptance criterion must be an object')
        prev = by_id.get(c.get('id')) if c.get('id') else None
        prev = prev or {}
        verifier_in = c.get('verifier') if isinstance(c.get('verifier'), dict) else {}
        verifier_prev = prev.get('verifier') if isinstance(prev.get('verifier'), dict) else {}
        item: dict[str, Any] = {
            'id': c.get('id') or f'criterion.{new_id()[:8]}',
            'statement': c.get('statement', prev.get('statement')),
            'required': c['required'] if 'required' in c and c['required'] is not None else prev.get('required', True),
            'verifier': {
                'kind': verifier_in.get('kind') or verifier_prev.get('kind') or 'inspection',
                'spec': verifier_in.get('spec') if verifier_in.get('spec') is not None else (verifier_prev.get('spec') if verifier_prev.get('spec') is not None else {}),
            },
            'status': c.get('status') or prev.get('status') or 'pending',
            'evidence_artifact_ids': list(c.get('evidence_artifact_ids') if c.get('evidence_artifact_ids') is not None else prev.get('evidence_artifact_ids') or []),
        }
        if not item['statement']:
            raise ValidationError('acceptance criterion needs a statement')
        waiver = c.get('waiver_reason') or prev.get('waiver_reason')
        if waiver:
            item['waiver_reason'] = waiver
        out.append(item)
    return out


def _setdefault(rec: dict[str, Any], key: str, value: Any) -> None:
    if rec.get(key) is None:
        rec[key] = value


def apply_defaults(type_: str, rec: dict[str, Any], actor: dict[str, Any], *, replica_id: str, seq: int,
                   workspace_revision: int, now_ms_value: float, new_id: Callable[[], str]) -> None:
    ts = rec['created_at']
    if type_ == 'work_item':
        _setdefault(rec, 'kind', 'task'); _setdefault(rec, 'status', 'backlog'); _setdefault(rec, 'priority', 'normal')
        _setdefault(rec, 'owner', clone(actor))
        for key in ('dependency_ids', 'blocked_by_ids', 'assignees', 'run_ids'):
            _setdefault(rec, key, [])
        rec['scopes'] = [normalize_scope(s) for s in (rec.get('scopes') or [])]
        rec['acceptance_criteria'] = normalize_criteria(rec.get('acceptance_criteria'), [], new_id)
        _setdefault(rec, 'description', '')
    elif type_ == 'run':
        _setdefault(rec, 'workflow_id', 'workflow.acheron.dispatch'); _setdefault(rec, 'workflow_revision', '1')
        _setdefault(rec, 'status', 'running'); _setdefault(rec, 'work_item_ids', []); _setdefault(rec, 'task_ids', [])
        _setdefault(rec, 'initiated_by', clone(actor)); _setdefault(rec, 'started_at', ts)
        _setdefault(rec, 'event_cursors', {replica_id: seq}); _setdefault(rec, 'metrics', {})
    elif type_ == 'task':
        _setdefault(rec, 'status', 'claimed'); _setdefault(rec, 'owner', clone(actor)); _setdefault(rec, 'attempt', 1)
        _setdefault(rec, 'max_attempts', 3)
        rec['scopes'] = [normalize_scope(s) for s in (rec.get('scopes') or [])]
        for key in ('dependency_task_ids', 'lease_ids', 'evidence_artifact_ids'):
            _setdefault(rec, key, [])
        _setdefault(rec, 'started_at', ts); _setdefault(rec, 'idempotency_key', rec['id'])
    elif type_ == 'lease':
        _setdefault(rec, 'mode', 'exclusive'); _setdefault(rec, 'status', 'active'); _setdefault(rec, 'acquired_at', ts)
        _setdefault(rec, 'heartbeat_at', ts); _setdefault(rec, 'expires_at', iso_from_ms(now_ms_value + DEFAULT_LEASE_TTL_MS))
        if rec.get('resource'):
            rec['resource'] = normalize_scope(rec['resource'])
    elif type_ == 'session':
        _setdefault(rec, 'kind', 'interactive'); _setdefault(rec, 'status', 'starting'); _setdefault(rec, 'actor', clone(actor))
        _setdefault(rec, 'task_ids', []); _setdefault(rec, 'started_at', ts); _setdefault(rec, 'event_cursors', {})
        _setdefault(rec, 'replica_id', replica_id)
    elif type_ == 'artifact':
        _setdefault(rec, 'kind', 'document'); _setdefault(rec, 'media_type', 'text/plain'); _setdefault(rec, 'immutable', True)
        _setdefault(rec, 'status', 'available'); _setdefault(rec, 'produced_by', clone(actor)); _setdefault(rec, 'metadata', {})
    elif type_ == 'context_manifest':
        _setdefault(rec, 'compiler', {'name': 'charon-overseer', 'version': KERNEL_VERSION})
        _setdefault(rec, 'workspace_revision', workspace_revision or 1)
        _setdefault(rec, 'event_cursors', {replica_id: seq}); _setdefault(rec, 'built_at', ts); _setdefault(rec, 'query', {})
        _setdefault(rec, 'budget', {'unit': 'byte', 'limit': 0, 'used': 0})
        for key in ('policy_ids', 'entries', 'exclusions'):
            _setdefault(rec, key, [])
        _setdefault(rec, 'digest', digest_of(canonical_json(rec['entries'])))
    elif type_ == 'policy':
        _setdefault(rec, 'version', '1'); _setdefault(rec, 'status', 'active'); _setdefault(rec, 'priority', 100)
        _setdefault(rec, 'effect', 'allow'); _setdefault(rec, 'actions', []); _setdefault(rec, 'subject_selectors', ['*'])
        rec['resource_selectors'] = [normalize_scope(s) for s in (rec.get('resource_selectors') or [])]
        _setdefault(rec, 'conditions', {}); _setdefault(rec, 'rationale', rec.get('name') or 'policy')
    elif type_ == 'knowledge_item':
        _setdefault(rec, 'kind', 'note'); _setdefault(rec, 'status', 'proposed'); _setdefault(rec, 'confidence', 0.5)
        _setdefault(rec, 'review', {'required': True, 'status': 'pending'})
        for key in ('scopes', 'evidence_artifact_ids', 'tags'):
            _setdefault(rec, key, [])
        _setdefault(rec, 'body', rec.get('title'))
    elif type_ == 'replica':
        _setdefault(rec, 'kind', 'local'); _setdefault(rec, 'trust', 'trusted'); _setdefault(rec, 'cursors', {})
        _setdefault(rec, 'capabilities', []); _setdefault(rec, 'policy_ids', [])
    elif type_ == 'proposal':
        _setdefault(rec, 'status', 'proposed'); _setdefault(rec, 'alternatives', []); _setdefault(rec, 'refs', [])
        _setdefault(rec, 'proposed_by', clone(actor))
    elif type_ == 'gate':
        _setdefault(rec, 'status', 'open'); _setdefault(rec, 'options', []); _setdefault(rec, 'refs', [])
        _setdefault(rec, 'opened_by', clone(actor))
        if 'answer' not in rec:
            rec['answer'] = None


def validate_record(type_: str, rec: dict[str, Any]) -> None:
    def need(key: str) -> None:
        value = rec.get(key)
        if value is None or value == '':
            raise ValidationError(f'{type_}.{key} is required')

    def one_of(key: str, values: Iterable[str]) -> None:
        allowed = list(values)
        if rec.get(key) not in allowed:
            raise ValidationError(f'{type_}.{key} "{rec.get(key)}" not in {"|".join(allowed)}')

    if type_ == 'work_item':
        need('title'); one_of('kind', WORK_ITEM_KINDS); one_of('status', FSM['work_item'].keys()); one_of('priority', PRIORITIES)
    elif type_ == 'task':
        need('work_item_id'); need('run_id'); need('title'); need('instruction'); one_of('status', FSM['task'].keys())
    elif type_ == 'run':
        if not rec.get('work_item_ids'):
            raise ValidationError('run.work_item_ids must not be empty')
    elif type_ == 'lease':
        need('resource'); need('owner_task_id'); need('fencing_token'); one_of('mode', ['shared', 'exclusive'])
    elif type_ == 'session':
        one_of('status', SESSION_STATUSES); one_of('kind', SESSION_KINDS)
    elif type_ == 'artifact':
        need('title'); need('locator'); need('digest')
    elif type_ == 'context_manifest':
        need('task_id')
    elif type_ == 'policy':
        need('name'); one_of('effect', ['allow', 'deny', 'require_approval'])
        if not rec.get('actions'):
            raise ValidationError('policy.actions must not be empty')
    elif type_ == 'knowledge_item':
        need('title'); need('body')
    elif type_ == 'replica':
        need('name'); need('identity')
    elif type_ == 'proposal':
        need('title'); need('rationale'); one_of('kind', PROPOSAL_KINDS)
    elif type_ == 'gate':
        need('question'); one_of('kind', GATE_KINDS)


def pick(obj: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    return {k: obj[k] for k in keys if k in obj and obj[k] is not None}


def is_path_prefix(a: str, b: str) -> bool:
    prefix = a if a.endswith('/') else a + '/'
    return len(b) > len(a) and b.startswith(prefix)


def scopes_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get('kind') != b.get('kind'):
        return False
    if a.get('selector') == b.get('selector'):
        return True
    if a.get('recursive') and is_path_prefix(a['selector'], b['selector']):
        return True
    if b.get('recursive') and is_path_prefix(b['selector'], a['selector']):
        return True
    return False


def criteria_progress(rec: dict[str, Any]) -> dict[str, int]:
    criteria = rec.get('acceptance_criteria') or []
    required = [c for c in criteria if c.get('required')]
    met = [c for c in required if (c.get('status') == 'passed' and (c.get('evidence_artifact_ids') or []))
           or (c.get('status') == 'waived' and c.get('waiver_reason'))]
    if required:
        percent = int(round(len(met) / len(required) * 100))
    else:
        percent = 100 if rec.get('status') == 'done' else 0
    return {'required': len(required), 'met': len(met), 'total': len(criteria), 'percent': percent}


def unmet_required_criteria(rec: dict[str, Any]) -> list[str]:
    unmet = []
    for c in rec.get('acceptance_criteria') or []:
        if not c.get('required'):
            continue
        if c.get('status') == 'passed' and (c.get('evidence_artifact_ids') or []):
            continue
        if c.get('status') == 'waived' and c.get('waiver_reason'):
            continue
        suffix = ', no evidence' if c.get('status') == 'passed' else ''
        unmet.append(f"{c.get('id')} [{c.get('status')}{suffix}] {c.get('statement')}")
    return unmet
