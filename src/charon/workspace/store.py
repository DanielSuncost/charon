"""``WorkspaceStore`` — durable, revisioned, hash-chained workspace records.

Persists the Workspace RFC storage layout (records/<type>.json maps, one hash-linked
``events/<replica>.jsonl`` per replica, artifacts by digest, immutable manifests) and
drives every lifecycle change through ``charon.orchestration.fsm`` (``machines.py``).
Behaviour mirrors Acheron's ``src/overseer/kernel.js`` so both implementations read and
write the same directories and bundles.
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from charon.orchestration import fsm
from charon.orchestration.fsm_store import _atomic_write_json

from . import records as R
from .machines import GUARD_PREFIX, SPECS, is_terminal
from .records import (
    BASE_FIELDS, BUNDLE_KEY, DEFAULT_LEASE_TTL_MS, EVENT_TYPE_RE, FSM, ID_PREFIX, KERNEL_VERSION, RECORD_TYPES,
    SCHEMA_TYPES, SCHEMA_VERSION, SESSION_STATUSES, SLUG_RE, SYSTEM_ACTOR, TYPE_FIELDS, USER_ACTOR,
    EventCollision, GuardFailed, IllegalTransition, LeaseConflict, NotFound, RevisionConflict, ValidationError,
    assert_id, canonical_json, clone, iso_from_ms, new_uuid, normalize_criteria, normalize_scope,
    now_ms, parse_iso_ms, pick, scopes_overlap, sha256_hex,
)

Actor = dict[str, Any]


def _content_hash(event: dict[str, Any]) -> str:
    return sha256_hex(canonical_json({'event_type': event.get('event_type'), 'subject_refs': event.get('subject_refs'),
                                      'payload': event.get('payload')}))


def event_hash(event: dict[str, Any]) -> str:
    """``sha256(previous_digest.value + canonical_json(event without digest))``."""
    rest = {k: v for k, v in event.items() if k != 'digest'}
    prev = (event.get('previous_digest') or {}).get('value') or ''
    return sha256_hex(prev + canonical_json(rest))


class WorkspaceStore:
    """One workspace directory; see the module docstring and README for the contract."""

    # ── construction ──────────────────────────────────────────────────────
    def __init__(self, root: Path | str, *, workspace_id: str, replica_id: str, slug: str | None = None,
                 title: str | None = None, roots: Iterable[dict[str, Any]] | None = None, description: str | None = None,
                 now: Callable[[], float] | None = None, new_id: Callable[[], str] | None = None):
        self.root = Path(root)
        self.workspace_id = assert_id(workspace_id, 'workspace_id')
        self.replica_id = assert_id(replica_id, 'replica_id')
        self._opts = {'slug': slug, 'title': title, 'roots': list(roots or []), 'description': description}
        self.now: Callable[[], float] = now or now_ms
        self.new_id: Callable[[], str] = new_id or new_uuid
        self.records: dict[str, dict[str, dict[str, Any]]] = {t: {} for t in RECORD_TYPES}
        self.workspace: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []          # every event known (all replicas), load order
        self.own_events: list[dict[str, Any]] = []      # this replica's chain
        self._events_by_id: dict[str, tuple[dict[str, Any], str]] = {}
        self.seq = 0
        self.last_digest: dict[str, str] | None = None
        self._own_events_file: Path | None = None
        self._dirty_types: set[str] = set()
        self._dirty_workspace = False
        self._pending_events: list[dict[str, Any]] = []
        self._pending_files: dict[str, str] = {}
        self._batch_depth = 0
        self._lock = threading.RLock()

    @classmethod
    def open(cls, root: Path | str, *, workspace_id: str, replica_id: str, slug: str | None = None, title: str | None = None,
             roots: Iterable[dict[str, Any]] | None = None, description: str | None = None,
             now: Callable[[], float] | None = None, new_id: Callable[[], str] | None = None) -> 'WorkspaceStore':
        store = cls(root, workspace_id=workspace_id, replica_id=replica_id, slug=slug, title=title, roots=roots,
                    description=description, now=now, new_id=new_id)
        store._load()
        return store

    @classmethod
    def import_bundle(cls, root: Path | str, bundle: dict[str, Any], *, replica_id: str | None = None,
                      now: Callable[[], float] | None = None, new_id: Callable[[], str] | None = None) -> 'WorkspaceStore':
        from .bundle import import_bundle
        return import_bundle(root, bundle, replica_id=replica_id, now=now, new_id=new_id)

    # ── time / ids ────────────────────────────────────────────────────────
    def iso(self, ms: float | None = None) -> str:
        return iso_from_ms(self.now() if ms is None else ms)

    def _gen_id(self, record_type: str) -> str:
        return f"{ID_PREFIX.get(record_type, record_type)}.{self.new_id()}"

    # ── persistence ───────────────────────────────────────────────────────
    def _path(self, rel: str) -> Path:
        return self.root / rel

    def _load(self) -> None:
        ws_path = self._path('workspace.json')
        for t in RECORD_TYPES:
            p = self._path(f'records/{t}.json')
            if p.exists():
                data = json.loads(p.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    self.records[t].update(data)
        events_dir = self._path('events')
        own_file = events_dir / f'{self.replica_id}.jsonl'
        if events_dir.exists():
            for file in sorted(events_dir.glob('*.jsonl')):
                for line in file.read_text(encoding='utf-8').splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    ev = json.loads(line)
                    self.events.append(ev)
                    self._events_by_id[ev['id']] = (ev, _content_hash(ev))
                    if ev.get('replica_id') == self.replica_id:
                        self.own_events.append(ev)
                        self.seq = max(self.seq, int(ev.get('replica_sequence') or 0))
                        self.last_digest = ev.get('digest') or None
                        own_file = file
        self._own_events_file = own_file
        if ws_path.exists():
            self.workspace = json.loads(ws_path.read_text(encoding='utf-8'))
        else:
            with self._batch():
                self._bootstrap()

    def _bootstrap(self) -> None:
        ts = self.iso()
        slug = self._opts.get('slug')
        if not slug:
            slug = R.slugify(self.workspace_id.removeprefix('workspace.'))[:63]
        if not SLUG_RE.fullmatch(slug):
            raise ValidationError(f'workspace slug "{slug}" is invalid')
        roots = [{'kind': r.get('kind') or 'directory', 'locator': r['locator']} for r in self._opts['roots']] or \
                [{'kind': 'directory', 'locator': '.'}]
        self.workspace = {
            'record_type': 'workspace', 'id': self.workspace_id, 'workspace_id': self.workspace_id, 'revision': 1,
            'created_at': ts, 'updated_at': ts, 'labels': {}, 'provenance': [],
            'extensions': {'acheron': {'kernel_version': KERNEL_VERSION, 'fencing_counters': {}}},
            'slug': slug, 'title': self._opts.get('title') or slug, 'state': 'active', 'roots': roots,
            'home_replica_id': self.replica_id, 'default_policy_ids': [],
        }
        if self._opts.get('description'):
            self.workspace['description'] = self._opts['description']
        self._dirty_workspace = True
        self.append_event('record.created', actor=SYSTEM_ACTOR,
                          subject_refs=[{'record_type': 'workspace', 'id': self.workspace_id, 'revision': 1}],
                          payload={'record_type': 'workspace', 'slug': slug, 'title': self.workspace['title']})
        identity = f'charon:{self.replica_id}'.ljust(16, '0')
        self.create('replica', {
            'id': self.replica_id, 'name': 'Charon local replica', 'kind': 'local', 'trust': 'trusted', 'identity': identity,
            'cursors': {}, 'capabilities': ['event.append', 'artifact.read', 'artifact.write', 'task.dispatch'], 'policy_ids': [],
        }, actor=SYSTEM_ACTOR)

    @contextmanager
    def _batch(self) -> Iterator[None]:
        with self._lock:
            self._batch_depth += 1
            try:
                yield
            finally:
                self._batch_depth -= 1
                if self._batch_depth == 0:
                    self._save()

    def _save(self) -> None:
        for rel, content in list(self._pending_files.items()):
            path = self._path(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
            tmp.write_text(content, encoding='utf-8')
            os.replace(tmp, path)
        self._pending_files.clear()
        if self._dirty_workspace:
            self._dirty_workspace = False
            _atomic_write_json(self._path('workspace.json'), self.workspace)
        for t in sorted(self._dirty_types):
            _atomic_write_json(self._path(f'records/{t}.json'), self.records[t])
        self._dirty_types.clear()
        if self._pending_events:
            file = self._own_events_file or self._path(f'events/{self.replica_id}.jsonl')
            self._own_events_file = file
            file.parent.mkdir(parents=True, exist_ok=True)
            with file.open('a', encoding='utf-8') as handle:
                for ev in self._pending_events:
                    handle.write(json.dumps(ev, ensure_ascii=False, allow_nan=False) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            self._pending_events.clear()

    def _touch(self, record_type: str) -> None:
        self._dirty_types.add(record_type)

    # ── reads ─────────────────────────────────────────────────────────────
    def get(self, record_type: str, record_id: str) -> dict[str, Any] | None:
        if record_type not in self.records:
            raise ValidationError(f'unknown record type {record_type}')
        return self.records[record_type].get(record_id)

    def list(self, record_type: str, pred: Callable[[dict[str, Any]], bool] | None = None) -> list[dict[str, Any]]:
        if record_type not in self.records:
            raise ValidationError(f'unknown record type {record_type}')
        values = list(self.records[record_type].values())
        return [v for v in values if pred(v)] if pred else values

    def _require(self, record_type: str, record_id: str) -> dict[str, Any]:
        rec = self.get(record_type, record_id)
        if rec is None:
            raise NotFound(f'{record_type} {record_id} not found')
        return rec

    # ── events ────────────────────────────────────────────────────────────
    def append_event(self, event_type: str, *, actor: Actor | None = None, subject_refs: Iterable[dict[str, Any]] = (),
                     payload: dict[str, Any] | None = None, correlation_id: str | None = None, causation_id: str | None = None,
                     base_revision: int | None = None, event_id: str | None = None, occurred_at: str | None = None) -> dict[str, Any]:
        if not isinstance(event_type, str) or not EVENT_TYPE_RE.fullmatch(event_type):
            raise ValidationError(f'bad event_type {event_type}')
        actor = actor or SYSTEM_ACTOR
        refs = [pick(r, ['record_type', 'id', 'revision', 'digest']) for r in subject_refs]
        probe = {'event_type': event_type, 'subject_refs': refs, 'payload': payload or {}}
        if event_id:
            assert_id(event_id, 'event_id')
            prior = self._events_by_id.get(event_id)
            if prior is not None:
                if prior[1] == _content_hash(probe):
                    return prior[0]
                raise EventCollision(f'event {event_id} already exists with different content')
        with self._batch():
            ts = occurred_at or self.iso()
            seq = self.seq + 1
            ev: dict[str, Any] = {
                'record_type': 'event', 'id': event_id or f'evt.{self.new_id()}', 'workspace_id': self.workspace_id, 'revision': 1,
                'created_at': ts, 'updated_at': ts, 'labels': {}, 'provenance': [], 'extensions': {},
                'replica_id': self.replica_id, 'replica_sequence': seq, 'logical_time': seq, 'event_type': event_type,
                'occurred_at': ts, 'actor': {'kind': actor['kind'], 'id': actor['id']}, 'subject_refs': refs,
                'payload': clone(payload) or {},
                'previous_digest': dict(self.last_digest) if self.last_digest else None,
            }
            if correlation_id:
                ev['correlation_id'] = str(correlation_id)
            if causation_id:
                ev['causation_event_id'] = assert_id(causation_id, 'causation_id')
            if base_revision is not None:
                ev['base_revision'] = base_revision
            ev['digest'] = {'algorithm': 'sha256', 'value': event_hash(ev)}
            self.seq = seq
            self.last_digest = ev['digest']
            self.events.append(ev)
            self.own_events.append(ev)
            self._events_by_id[ev['id']] = (ev, _content_hash(ev))
            self._pending_events.append(ev)
            return ev

    def verify_chain(self, replica_id: str | None = None) -> dict[str, Any]:
        """Re-read a replica's jsonl from disk and recompute links, sequence and digests."""
        rid = replica_id or self.replica_id
        file = self._own_events_file if rid == self.replica_id and self._own_events_file else self._path(f'events/{rid}.jsonl')
        lines = file.read_text(encoding='utf-8').splitlines() if file and file.exists() else []
        prev: str | None = None
        count = 0
        expect_seq = 1
        for line in lines:
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                return {'ok': False, 'count': count, 'broken_at': count + 1, 'reason': 'unparseable line'}
            got = (ev.get('digest') or {}).get('value')
            link_ok = (prev is None and ev.get('previous_digest') is None) or \
                      (prev is not None and (ev.get('previous_digest') or {}).get('value') == prev)
            if not link_ok:
                return {'ok': False, 'count': count, 'broken_at': ev.get('replica_sequence'), 'reason': 'previous_digest link mismatch'}
            if ev.get('replica_sequence') != expect_seq:
                return {'ok': False, 'count': count, 'broken_at': ev.get('replica_sequence'), 'reason': f'sequence gap (expected {expect_seq})'}
            if event_hash(ev) != got:
                return {'ok': False, 'count': count, 'broken_at': ev.get('replica_sequence'), 'reason': 'digest mismatch'}
            prev = got
            count += 1
            expect_seq += 1
        return {'ok': True, 'count': count, 'broken_at': None, 'reason': None}

    # ── create / update / transition ──────────────────────────────────────
    def create(self, record_type: str, fields: dict[str, Any] | None = None, *, actor: Actor | None = None,
               event_id: str | None = None, provenance: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if record_type not in self.records:
            raise ValidationError(f'unknown record type {record_type}')
        fields = fields or {}
        actor = actor or SYSTEM_ACTOR
        with self._batch():
            ts = self.iso()
            rid = assert_id(fields['id']) if fields.get('id') else self._gen_id(record_type)
            if rid in self.records[record_type]:
                raise ValidationError(f'{record_type} {rid} already exists')
            rec: dict[str, Any] = {
                'record_type': record_type, 'id': rid, 'workspace_id': self.workspace_id, 'revision': 1, 'created_at': ts,
                'updated_at': ts, 'labels': clone(fields.get('labels')) or {},
                'provenance': clone(provenance if provenance is not None else fields.get('provenance')) or [],
                'extensions': clone(fields.get('extensions')) or {},
            }
            known = set(TYPE_FIELDS.get(record_type, []))
            for key, value in fields.items():
                if key in BASE_FIELDS:
                    continue
                if key in known:
                    rec[key] = clone(value)
                else:
                    rec['extensions'][key] = clone(value)
            R.apply_defaults(record_type, rec, actor, replica_id=self.replica_id, seq=self.seq,
                             workspace_revision=int(self.workspace.get('revision') or 1), now_ms_value=self.now(), new_id=self.new_id)
            R.validate_record(record_type, rec)
            self.records[record_type][rid] = rec
            self._touch(record_type)
            if record_type == 'context_manifest':
                self._pending_files[f'manifests/{rid}.json'] = json.dumps(rec, ensure_ascii=False, indent=2)
            payload = {'record_type': record_type, **pick(rec, ['kind', 'title', 'status', 'work_item_id', 'task_id', 'session_id', 'mode']),
                       'record_digest': sha256_hex(canonical_json(rec))}
            self.append_event('record.created', actor=actor, event_id=event_id,
                              subject_refs=[{'record_type': record_type, 'id': rid, 'revision': 1}], payload=payload)
            return rec

    def _commit(self, record_type: str, rec: dict[str, Any], base_revision: int, *, actor: Actor, event_id: str | None,
                event_type: str, payload: dict[str, Any], causation_id: str | None = None,
                correlation_id: str | None = None) -> dict[str, Any]:
        rec['revision'] = base_revision + 1
        rec['updated_at'] = self.iso()
        self._touch(record_type)
        self.append_event(event_type, actor=actor, event_id=event_id, causation_id=causation_id, correlation_id=correlation_id,
                          subject_refs=[{'record_type': record_type, 'id': rec['id'], 'revision': rec['revision']}],
                          base_revision=base_revision, payload={'record_type': record_type, **payload})
        return rec

    def update(self, record_type: str, record_id: str, expected_revision: int, patch: dict[str, Any] | None = None, *,
               actor: Actor | None = None, event_id: str | None = None) -> dict[str, Any]:
        actor = actor or SYSTEM_ACTOR
        patch = patch or {}
        with self._batch():
            rec = self._require(record_type, record_id)
            if rec['revision'] != expected_revision:
                raise RevisionConflict(f'{record_type} {record_id} is at revision {rec["revision"]}, expected {expected_revision}',
                                       revision=rec['revision'])
            if 'status' in patch and patch['status'] is not None and record_type in FSM:
                raise ValidationError(f'{record_type}.status changes only through transition()')
            known = set(TYPE_FIELDS.get(record_type, []))
            changed: list[str] = []
            for key, value in patch.items():
                if key in ('record_type', 'id', 'workspace_id', 'revision', 'created_at', 'updated_at'):
                    continue
                if key == 'extensions':
                    rec['extensions'].update(clone(value) or {})
                    changed.append(key)
                    continue
                if key == 'labels':
                    rec['labels'] = clone(value) or {}
                    changed.append(key)
                    continue
                if key == 'provenance':
                    rec['provenance'] = clone(value) or []
                    changed.append(key)
                    continue
                if key == 'acceptance_criteria' and record_type == 'work_item':
                    rec['acceptance_criteria'] = normalize_criteria(value, rec.get('acceptance_criteria'), self.new_id)
                    changed.append(key)
                    continue
                if key == 'scopes' and isinstance(value, list):
                    rec['scopes'] = [normalize_scope(s) for s in value]
                    changed.append(key)
                    continue
                if key in known:
                    if value is None:
                        rec.pop(key, None)
                    else:
                        rec[key] = clone(value)
                else:
                    rec['extensions'][key] = clone(value)
                changed.append(key)
            R.validate_record(record_type, rec)
            return self._commit(record_type, rec, expected_revision, actor=actor, event_id=event_id,
                                event_type='record.updated', payload={'changed': changed})

    def transition(self, record_type: str, record_id: str, expected_revision: int, event: str, *, actor: Actor | None = None,
                   reason: str | None = None, event_id: str | None = None, patch: dict[str, Any] | None = None) -> dict[str, Any]:
        spec = SPECS.get(record_type)
        if spec is None:
            raise ValidationError(f'{record_type} has no lifecycle')
        actor = actor or SYSTEM_ACTOR
        with self._batch():
            rec = self._require(record_type, record_id)
            source = rec.get('status')
            table = FSM[record_type]
            # Idempotent replay: the same event id, record, event, reason and base revision as an
            # already-recorded transition is a no-op (even with a now-stale expected_revision);
            # anything else under a used id is a collision.
            if event_id and event_id in self._events_by_id:
                prior, _ = self._events_by_id[event_id]
                refs = prior.get('subject_refs') or [{}]
                same = (prior.get('event_type') == 'record.transitioned' and refs[0].get('record_type') == record_type
                        and refs[0].get('id') == record_id and refs[0].get('revision') == expected_revision + 1
                        and (prior.get('payload') or {}).get('event') == event
                        and (prior.get('payload') or {}).get('reason') == reason)
                if same:
                    return rec
                raise EventCollision(f'event {event_id} already exists with different content')
            if rec['revision'] != expected_revision:
                raise RevisionConflict(f'{record_type} {record_id} is at revision {rec["revision"]}, expected {expected_revision}',
                                       revision=rec['revision'])
            if source not in table:
                raise IllegalTransition(f'{record_type} {record_id} has unknown status "{source}"')
            if is_terminal(record_type, source):
                raise IllegalTransition(f'{record_type} {record_id} is in terminal state "{source}"')
            ts = self.iso()
            instance = fsm.MachineInstance(machine=spec.name, machine_version=spec.version, instance_id=record_id, state=source,
                                           data=clone(rec), revision=rec['revision'], created_at=rec['created_at'],
                                           updated_at=rec['updated_at'])
            try:
                result = fsm.dispatch(spec, instance, event, event_id=event_id or f'evt.{self.new_id()}',
                                      payload={'reason': reason, 'actor': clone(actor), 'ts': ts},
                                      expected_revision=rec['revision'], now=ts)
            except fsm.RevisionConflict as exc:
                raise RevisionConflict(str(exc), revision=rec['revision']) from exc
            except fsm.TransitionRejected as exc:
                message = str(exc)
                if GUARD_PREFIX in message:
                    unmet = R.unmet_required_criteria(rec)
                    raise GuardFailed(message.split(GUARD_PREFIX, 1)[1], unmet=[u.split(' ', 1)[0] for u in unmet]) from exc
                legal = ', '.join(table.get(source, {}).keys()) or 'none'
                raise IllegalTransition(f'{record_type} {record_id}: event "{event}" is not legal from "{source}" (legal: {legal})') from exc
            except fsm.EventCollision as exc:
                raise EventCollision(str(exc)) from exc
            new_state = result.instance.state
            for key, value in result.instance.data.items():
                if key not in BASE_FIELDS:
                    rec[key] = value
            rec['status'] = new_state
            if isinstance(patch, dict):
                known = set(TYPE_FIELDS.get(record_type, []))
                for key, value in patch.items():
                    if key == 'extensions':
                        rec['extensions'].update(clone(value) or {})
                    elif key in known:
                        rec[key] = clone(value)
                    else:
                        rec['extensions'][key] = clone(value)
            return self._commit(record_type, rec, expected_revision, actor=actor, event_id=event_id,
                                event_type='record.transitioned',
                                payload={'from': source, 'to': new_state, 'event': event, 'reason': reason})

    # ── criteria / sessions ───────────────────────────────────────────────
    def attach_evidence(self, work_item_id: str, expected_revision: int, criterion_id: str, *, status: str = 'passed',
                        artifact_ids: Iterable[str] = (), task_id: str | None = None, waiver_reason: str | None = None,
                        actor: Actor | None = None, event_id: str | None = None) -> dict[str, Any]:
        actor = actor or SYSTEM_ACTOR
        artifact_ids = list(artifact_ids)
        with self._batch():
            rec = self._require('work_item', work_item_id)
            if rec['revision'] != expected_revision:
                raise RevisionConflict(f'work_item {work_item_id} is at revision {rec["revision"]}, expected {expected_revision}',
                                       revision=rec['revision'])
            criterion = next((c for c in rec.get('acceptance_criteria') or [] if c.get('id') == criterion_id), None)
            if criterion is None:
                raise NotFound(f'criterion {criterion_id} not on {work_item_id}')
            if status not in ('passed', 'failed', 'waived', 'pending'):
                raise ValidationError(f'bad criterion status {status}')
            if status == 'waived' and not waiver_reason:
                raise ValidationError('waiving a criterion requires waiver_reason')
            if status == 'passed' and not artifact_ids and not criterion.get('evidence_artifact_ids'):
                raise ValidationError('passing a criterion requires at least one evidence artifact')
            for aid in artifact_ids:
                assert_id(aid)
                if aid not in criterion['evidence_artifact_ids']:
                    criterion['evidence_artifact_ids'].append(aid)
            criterion['status'] = status
            if waiver_reason:
                criterion['waiver_reason'] = waiver_reason
            if task_id:
                task = self.get('task', task_id)
                if task is not None:
                    for aid in artifact_ids:
                        if aid not in task['evidence_artifact_ids']:
                            task['evidence_artifact_ids'].append(aid)
                    self._touch('task')
            return self._commit('work_item', rec, expected_revision, actor=actor, event_id=event_id, event_type='evidence.attached',
                                payload={'criterion_id': criterion_id, 'status': status, 'artifact_ids': artifact_ids,
                                         'task_id': task_id or None})

    def set_session_status(self, session_id: str, status: str, *, actor: Actor | None = None,
                           extensions: dict[str, Any] | None = None, event_id: str | None = None) -> dict[str, Any]:
        actor = actor or SYSTEM_ACTOR
        with self._batch():
            rec = self._require('session', session_id)
            if status not in SESSION_STATUSES:
                raise ValidationError(f'bad session status {status}')
            changed = rec.get('status') != status
            base = rec['revision']
            if extensions:
                rec['extensions'].update(clone(extensions))
            if not changed:
                rec['updated_at'] = self.iso()
                self._touch('session')
                return rec
            source = rec.get('status')
            rec['status'] = status
            if status in ('completed', 'failed', 'stopped'):
                rec['ended_at'] = self.iso()
            return self._commit('session', rec, base, actor=actor, event_id=event_id, event_type='session.status',
                                payload={'from': source, 'to': status})

    # ── artifacts ─────────────────────────────────────────────────────────
    def put_artifact(self, *, text: str | None = None, data: bytes | None = None, kind: str = 'document', title: str | None = None,
                     media_type: str = 'text/plain', produced_by: Actor | None = None, task_id: str | None = None,
                     run_id: str | None = None, session_id: str | None = None, metadata: dict[str, Any] | None = None,
                     source_revision: str | None = None, actor: Actor | None = None, event_id: str | None = None) -> dict[str, Any]:
        content = text
        if content is None and data is not None:
            content = data.decode('utf-8') if isinstance(data, (bytes, bytearray)) else str(data)
        if not isinstance(content, str):
            raise ValidationError('put_artifact needs text or data')
        produced_by = produced_by or SYSTEM_ACTOR
        hex_digest = sha256_hex(content)
        rid = f'artifact.sha256:{hex_digest}'
        existing = self.get('artifact', rid)
        if existing is not None:
            return existing
        with self._batch():
            locator = f'workspace/artifacts/sha256/{hex_digest}'
            self._pending_files[f'artifacts/sha256/{hex_digest}'] = content
            fields: dict[str, Any] = {
                'id': rid, 'kind': kind, 'title': title or f'{kind} {hex_digest[:12]}', 'media_type': media_type, 'locator': locator,
                'digest': {'algorithm': 'sha256', 'value': hex_digest}, 'size_bytes': len(content.encode('utf-8')),
                'immutable': True, 'status': 'available', 'produced_by': produced_by, 'metadata': metadata or {},
            }
            if task_id:
                fields['task_id'] = task_id
            if run_id:
                fields['run_id'] = run_id
            if session_id:
                fields['session_id'] = session_id
            if source_revision:
                fields['source_revision'] = source_revision
            return self.create('artifact', fields, actor=actor or produced_by, event_id=event_id)

    def read_artifact(self, artifact_id: str) -> str | None:
        rec = self.get('artifact', artifact_id)
        if rec is None:
            return None
        rel = rec['locator'].removeprefix('workspace/')
        if rel in self._pending_files:
            return self._pending_files[rel]
        path = self._path(rel)
        return path.read_text(encoding='utf-8') if path.exists() else None

    # ── leases ────────────────────────────────────────────────────────────
    def _counters(self) -> dict[str, int]:
        ext = self.workspace.setdefault('extensions', {})
        acheron = ext.setdefault('acheron', {})
        return acheron.setdefault('fencing_counters', {})

    def find_lease_conflicts(self, scope: dict[str, Any], mode: str = 'exclusive', exclude_task_id: str | None = None) -> list[dict[str, Any]]:
        s = normalize_scope(scope)
        now = self.now()
        out = []
        for lease in self.records['lease'].values():
            if lease.get('status') != 'active':
                continue
            expires = parse_iso_ms(lease.get('expires_at'))
            if expires is not None and expires <= now:
                continue
            if exclude_task_id and lease.get('owner_task_id') == exclude_task_id:
                continue
            if not scopes_overlap(s, lease.get('resource') or {}):
                continue
            if mode == 'shared' and lease.get('mode') == 'shared':
                continue
            out.append(lease)
        return out

    def lease_acquire(self, *, task_id: str, session_id: str | None = None, scopes: Iterable[dict[str, Any]] = (),
                      mode: str = 'exclusive', ttl_ms: int = DEFAULT_LEASE_TTL_MS, actor: Actor | None = None,
                      event_id: str | None = None) -> list[dict[str, Any]]:
        if not task_id:
            raise ValidationError('lease_acquire needs task_id')
        if mode not in ('shared', 'exclusive'):
            raise ValidationError(f'bad lease mode {mode}')
        actor = actor or SYSTEM_ACTOR
        with self._batch():
            self.expire_leases(actor=actor)
            norm = [normalize_scope(s) for s in scopes]
            conflicts = []
            for s in norm:
                for lease in self.find_lease_conflicts(s, mode, exclude_task_id=task_id):
                    conflicts.append({'scope': s, 'lease': lease})
            if conflicts:
                self.append_event('lease.conflict', actor=actor,
                                  subject_refs=[{'record_type': 'task', 'id': task_id}] +
                                               [{'record_type': 'lease', 'id': c['lease']['id'], 'revision': c['lease']['revision']} for c in conflicts],
                                  payload={'task_id': task_id, 'session_id': session_id or None, 'mode': mode,
                                           'conflicts': [{'scope': c['scope'], 'lease_id': c['lease']['id'],
                                                          'owner_task_id': c['lease'].get('owner_task_id'),
                                                          'owner_session_id': c['lease'].get('owner_session_id') or None,
                                                          'fencing_token': c['lease'].get('fencing_token')} for c in conflicts]})
                summary = '; '.join(f"{c['scope']['selector']} held by {c['lease'].get('owner_task_id')}" for c in conflicts)
                raise LeaseConflict(f'{len(conflicts)} lease conflict(s): {summary}', conflicts)
            counters = self._counters()
            leases: list[dict[str, Any]] = []
            now = self.now()
            for s in norm:
                held = self.list('lease', lambda lease, s=s: lease.get('status') == 'active' and lease.get('owner_task_id') == task_id
                                 and (lease.get('resource') or {}).get('selector') == s['selector']
                                 and (lease.get('resource') or {}).get('kind') == s['kind'])
                if held:
                    leases.append(held[0])
                    continue
                counters[s['selector']] = int(counters.get(s['selector'], 0)) + 1
                self._dirty_workspace = True
                fields: dict[str, Any] = {'resource': s, 'mode': mode, 'owner_task_id': task_id, 'fencing_token': counters[s['selector']],
                                          'status': 'active', 'acquired_at': self.iso(now), 'heartbeat_at': self.iso(now),
                                          'expires_at': self.iso(now + ttl_ms)}
                if session_id:
                    fields['owner_session_id'] = session_id
                leases.append(self.create('lease', fields, actor=actor,
                                          event_id=f'{event_id}.{len(leases)}' if event_id else None))
            task = self.get('task', task_id)
            if task is not None:
                changed = False
                for lease in leases:
                    if lease['id'] not in task['lease_ids']:
                        task['lease_ids'].append(lease['id'])
                        changed = True
                if changed:
                    self._commit('task', task, task['revision'], actor=actor, event_id=None, event_type='record.updated',
                                 payload={'changed': ['lease_ids']})
            return leases

    def lease_heartbeat(self, lease_id: str, ttl_ms: int | None = None) -> dict[str, Any]:
        with self._batch():
            lease = self._require('lease', lease_id)
            if lease.get('status') != 'active':
                raise IllegalTransition(f'lease {lease_id} is {lease.get("status")}')
            now = self.now()
            lease['heartbeat_at'] = self.iso(now)
            lease['expires_at'] = self.iso(now + (ttl_ms or DEFAULT_LEASE_TTL_MS))
            lease['updated_at'] = lease['heartbeat_at']
            lease['revision'] += 1
            self._touch('lease')  # liveness, not a state transition: no event
            return lease

    def lease_release(self, lease_id: str, reason: str | None = None, *, actor: Actor | None = None,
                      event_id: str | None = None) -> dict[str, Any]:
        lease = self._require('lease', lease_id)
        return self.transition('lease', lease_id, lease['revision'], 'release', actor=actor, reason=reason, event_id=event_id)

    def expire_leases(self, *, actor: Actor | None = None) -> list[dict[str, Any]]:
        actor = actor or SYSTEM_ACTOR
        now = self.now()
        out = []
        with self._batch():
            for lease in list(self.records['lease'].values()):
                expires = parse_iso_ms(lease.get('expires_at'))
                if lease.get('status') == 'active' and expires is not None and expires <= now:
                    out.append(self.transition('lease', lease['id'], lease['revision'], 'expire', actor=actor, reason='ttl elapsed'))
        return out

    # ── gates / proposals (extension records) ─────────────────────────────
    def create_gate(self, *, kind: str = 'question', question: str, options: Iterable[str] = (), refs: Iterable[str] = (),
                    related: dict[str, Any] | None = None, actor: Actor | None = None, event_id: str | None = None) -> dict[str, Any]:
        actor = actor or SYSTEM_ACTOR
        with self._batch():
            gate = self.create('gate', {'kind': kind, 'question': question, 'options': list(options), 'refs': list(refs),
                                        'related': related, 'opened_by': actor}, actor=actor, event_id=event_id)
            self.append_event('gate.opened', actor=actor, subject_refs=[{'record_type': 'gate', 'id': gate['id'], 'revision': gate['revision']}],
                              payload={'kind': kind, 'question': question, 'options': list(options)})
            return gate

    def decide_gate(self, gate_id: str, decision: Any, actor: Actor | None = None, *, event_id: str | None = None) -> dict[str, Any]:
        actor = actor or USER_ACTOR
        with self._batch():
            gate = self._require('gate', gate_id)
            answer = decision if isinstance(decision, dict) else {'answer': decision}
            rec = self.transition('gate', gate_id, gate['revision'], 'answer', actor=actor, patch={'answer': answer})
            self.append_event('gate.decided', actor=actor, subject_refs=[{'record_type': 'gate', 'id': gate_id, 'revision': rec['revision']}],
                              payload={'kind': rec.get('kind'), 'answer': answer}, event_id=event_id)
            return rec

    def create_proposal(self, *, kind: str, title: str, rationale: str, alternatives: Iterable[str] = (), action: Any = None,
                        refs: Iterable[str] = (), actor: Actor | None = None, event_id: str | None = None) -> dict[str, Any]:
        actor = actor or SYSTEM_ACTOR
        return self.create('proposal', {'kind': kind, 'title': title, 'rationale': rationale, 'alternatives': list(alternatives),
                                        'action': action, 'refs': list(refs), 'proposed_by': actor}, actor=actor, event_id=event_id)

    def transition_proposal(self, proposal_id: str, event: str, actor: Actor | None = None, reason: str | None = None, *,
                            event_id: str | None = None) -> dict[str, Any]:
        actor = actor or USER_ACTOR
        proposal = self._require('proposal', proposal_id)
        return self.transition('proposal', proposal_id, proposal['revision'], event, actor=actor, reason=reason, event_id=event_id)

    # ── workspace extensions ──────────────────────────────────────────────
    def set_extension(self, key: str, value: Any) -> Any:
        with self._batch():
            acheron = self.workspace.setdefault('extensions', {}).setdefault('acheron', {})
            acheron[key] = clone(value)
            self._dirty_workspace = True
            return acheron[key]

    def get_extension(self, key: str) -> Any:
        return (self.workspace.get('extensions') or {}).get('acheron', {}).get(key)

    # ── export / projections ──────────────────────────────────────────────
    def export_bundle(self) -> dict[str, Any]:
        bundle: dict[str, Any] = {
            'schema_version': SCHEMA_VERSION, 'exported_at': self.iso(), 'exported_by_replica_id': self.replica_id,
            'workspace': clone(self.workspace),
            'entities': [], 'relations': [], 'observations': [], 'knowledge_items': [], 'work_items': [], 'runs': [], 'tasks': [],
            'leases': [], 'sessions': [], 'artifacts': [], 'skills': [], 'policies': [], 'context_manifests': [], 'replicas': [],
            'conflicts': [], 'events': [clone(e) for e in self.events],
            'extensions': {'acheron': {'kernel_version': KERNEL_VERSION,
                                       'proposals': [clone(p) for p in self.list('proposal')],
                                       'gates': [clone(g) for g in self.list('gate')]}},
        }
        for t in SCHEMA_TYPES:
            bundle[BUNDLE_KEY[t]] = [clone(r) for r in self.list(t)]
        return bundle

    def projection(self) -> dict[str, Any]:
        from .projections import build_projection
        return build_projection(self)

    def write_projections(self, status: bool = True) -> None:
        from .projections import render_plan_md, render_status_md
        _atomic_write_json(self._path('exports/current.bundle.json'), self.export_bundle())
        _atomic_write_json(self._path('projection.json'), self.projection())
        if status:
            self._path('PROJECT_STATUS.md').write_text(render_status_md(self), encoding='utf-8')
            self._path('PLAN.md').write_text(render_plan_md(self), encoding='utf-8')
