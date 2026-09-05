"""``charon.workspace`` — Workspace RFC Increment 0: durable, revisioned, hash-chained
records (work items, runs, tasks, leases, sessions, artifacts, manifests, knowledge,
gates, proposals) with lifecycles on ``charon.orchestration.fsm``.  Interchangeable with
Acheron's overseer kernel (same layout, same bundle, same event chain).
"""
from .bundle import import_bundle, read_bundle, records_equal, validate_bundle, write_bundle_files
from .policy import evaluate_send_policy, evaluate_spawn_policy, looks_like_approval, redact
from .projections import build_projection, render_plan_md, render_status_md, summarize_work
from .records import (
    FSM, KERNEL_VERSION, RECORD_TYPES, SCHEMA_VERSION, SESSION_STATUSES, SYSTEM_ACTOR, USER_ACTOR,
    EventCollision, GuardFailed, IllegalTransition, LeaseConflict, NotFound, RevisionConflict, ValidationError, WorkspaceError,
    canonical_json, iso_from_ms, new_uuid, sha256_hex,
)
from .store import WorkspaceStore, event_hash

__all__ = [
    'WorkspaceStore', 'event_hash',
    'WorkspaceError', 'RevisionConflict', 'IllegalTransition', 'GuardFailed', 'LeaseConflict', 'EventCollision', 'NotFound', 'ValidationError',
    'canonical_json', 'sha256_hex', 'iso_from_ms', 'new_uuid',
    'FSM', 'RECORD_TYPES', 'SESSION_STATUSES', 'SCHEMA_VERSION', 'KERNEL_VERSION', 'SYSTEM_ACTOR', 'USER_ACTOR',
    'import_bundle', 'read_bundle', 'validate_bundle', 'write_bundle_files', 'records_equal',
    'build_projection', 'render_status_md', 'render_plan_md', 'summarize_work',
    'evaluate_send_policy', 'evaluate_spawn_policy', 'looks_like_approval', 'redact',
]
