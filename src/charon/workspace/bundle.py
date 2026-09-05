"""Bundle export / import / validation for a workspace directory.

A bundle is the Workspace contract's portability document
(``docs/contracts/workspace.schema.json``).  Import writes the RFC storage layout
(records, per-replica event chains, workspace.json) and opens a ``WorkspaceStore`` on it;
export is ``WorkspaceStore.export_bundle``.  Artifact bytes are not carried by bundles
(only their records); ``artifacts/`` is copied separately when moving a directory.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from charon.orchestration.fsm_store import _atomic_write_json

from .records import BUNDLE_KEY, SCHEMA_TYPES, ValidationError

SCHEMA_PATH = Path(__file__).resolve().parents[3] / 'docs' / 'contracts' / 'workspace.schema.json'


def load_schema(path: Path | str | None = None) -> dict[str, Any]:
    return json.loads(Path(path or SCHEMA_PATH).read_text(encoding='utf-8'))


def validate_bundle(bundle: dict[str, Any], *, schema: dict[str, Any] | None = None,
                    schema_path: Path | str | None = None) -> list[str]:
    """Return a list of human-readable schema errors (empty = valid).

    Uses ``jsonschema`` (Draft 2020-12 with format checking) when it is installed; without it
    only structural checks run and a single note is returned so callers know validation was partial.
    """
    try:
        import jsonschema  # type: ignore
    except Exception:  # pragma: no cover - environment without jsonschema
        errors = []
        for key in ('schema_version', 'exported_at', 'exported_by_replica_id', 'workspace', 'events'):
            if key not in bundle:
                errors.append(f'$.{key}: missing')
        errors.append('note: jsonschema is not installed; only structural checks ran')
        return errors
    schema = schema or load_schema(schema_path)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    return [f'{e.json_path}: {e.message}' for e in validator.iter_errors(bundle)]


def write_bundle_files(root: Path | str, bundle: dict[str, Any]) -> Path:
    """Materialize a bundle as the storage layout under ``root`` (no store opened)."""
    root = Path(root)
    if not isinstance(bundle, dict) or not isinstance(bundle.get('workspace'), dict):
        raise ValidationError('bundle.workspace is required')
    (root / 'records').mkdir(parents=True, exist_ok=True)
    (root / 'events').mkdir(parents=True, exist_ok=True)
    (root / 'artifacts' / 'sha256').mkdir(parents=True, exist_ok=True)
    (root / 'manifests').mkdir(parents=True, exist_ok=True)
    (root / 'exports').mkdir(parents=True, exist_ok=True)
    _atomic_write_json(root / 'workspace.json', bundle['workspace'])
    for record_type in SCHEMA_TYPES:
        items = bundle.get(BUNDLE_KEY[record_type]) or []
        _atomic_write_json(root / 'records' / f'{record_type}.json', {r['id']: r for r in items})
    ext = (bundle.get('extensions') or {}).get('acheron') or {}
    _atomic_write_json(root / 'records' / 'proposal.json', {r['id']: r for r in ext.get('proposals') or []})
    _atomic_write_json(root / 'records' / 'gate.json', {r['id']: r for r in ext.get('gates') or []})
    for manifest in bundle.get('context_manifests') or []:
        _atomic_write_json(root / 'manifests' / f"{manifest['id']}.json", manifest)
    by_replica: dict[str, list[dict[str, Any]]] = {}
    for event in bundle.get('events') or []:
        by_replica.setdefault(str(event.get('replica_id') or 'unknown'), []).append(event)
    for replica_id, events in by_replica.items():
        events.sort(key=lambda e: int(e.get('replica_sequence') or 0))
        path = root / 'events' / f'{replica_id}.jsonl'
        path.write_text(''.join(json.dumps(e, ensure_ascii=False, allow_nan=False) + '\n' for e in events), encoding='utf-8')
    return root


def import_bundle(root: Path | str, bundle: dict[str, Any], *, replica_id: str | None = None,
                  now: Callable[[], float] | None = None, new_id: Callable[[], str] | None = None):
    from .store import WorkspaceStore
    root = write_bundle_files(root, bundle)
    ws = bundle['workspace']
    rid = replica_id or bundle.get('exported_by_replica_id') or ws.get('home_replica_id')
    return WorkspaceStore.open(root, workspace_id=ws['id'], replica_id=rid, slug=ws.get('slug'), title=ws.get('title'),
                               roots=ws.get('roots'), now=now, new_id=new_id)


def read_bundle(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def records_equal(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Record-by-record comparison of two bundles (ignoring export metadata); returns differences."""
    diffs: list[str] = []
    keys = [BUNDLE_KEY[t] for t in SCHEMA_TYPES] + ['events']
    for key in keys:
        left = {r['id']: r for r in a.get(key) or []}
        right = {r['id']: r for r in b.get(key) or []}
        if set(left) != set(right):
            diffs.append(f'{key}: ids differ ({sorted(set(left) ^ set(right))[:5]})')
            continue
        for rid, rec in left.items():
            if rec != right[rid]:
                diffs.append(f'{key}/{rid}: content differs')
    for key in ('proposals', 'gates'):
        left = (a.get('extensions') or {}).get('acheron', {}).get(key) or []
        right = (b.get('extensions') or {}).get('acheron', {}).get(key) or []
        if left != right:
            diffs.append(f'extensions.acheron.{key}: differs')
    if a.get('workspace') != b.get('workspace'):
        diffs.append('workspace: differs')
    return diffs
