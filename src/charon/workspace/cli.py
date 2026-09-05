"""``python -m charon.workspace {export,import,validate,status,verify} --root <dir>``"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .bundle import import_bundle, read_bundle, validate_bundle
from .projections import summarize_work
from .store import WorkspaceStore


def _open_existing(root: Path, replica_id: str | None) -> WorkspaceStore:
    ws_path = root / 'workspace.json'
    if not ws_path.exists():
        raise SystemExit(f'no workspace at {root} (missing workspace.json)')
    ws = json.loads(ws_path.read_text(encoding='utf-8'))
    return WorkspaceStore.open(root, workspace_id=ws['id'], replica_id=replica_id or ws.get('home_replica_id'),
                               slug=ws.get('slug'), title=ws.get('title'), roots=ws.get('roots'))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='charon.workspace', description='Workspace records: export, import, validate, status, verify')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('export', 'import', 'validate', 'status', 'verify'):
        p = sub.add_parser(name)
        p.add_argument('--root', required=name != 'validate', help='workspace directory')
        p.add_argument('--replica-id', default=None)
        if name in ('export', 'import', 'validate'):
            p.add_argument('--bundle', default=None, help='bundle file (export: output, default stdout; import/validate: input)')
    args = parser.parse_args(argv)
    root = Path(args.root) if getattr(args, 'root', None) else None

    if args.command == 'export':
        store = _open_existing(root, args.replica_id)
        bundle = store.export_bundle()
        text = json.dumps(bundle, indent=2, ensure_ascii=False) + '\n'
        if args.bundle:
            Path(args.bundle).write_text(text, encoding='utf-8')
            print(f'exported {len(bundle["events"])} events to {args.bundle}')
        else:
            sys.stdout.write(text)
        return 0
    if args.command == 'import':
        if not args.bundle:
            raise SystemExit('--bundle is required for import')
        bundle = read_bundle(args.bundle)
        errors = validate_bundle(bundle)
        if errors:
            print('bundle is not valid:', file=sys.stderr)
            for e in errors[:20]:
                print(f'  {e}', file=sys.stderr)
            return 2
        store = import_bundle(root, bundle, replica_id=args.replica_id)
        chain = store.verify_chain()
        print(f'imported into {root}: {sum(len(v) for v in store.records.values())} records, {len(store.events)} events; chain {chain}')
        return 0 if chain['ok'] else 3
    if args.command == 'validate':
        source = args.bundle
        if not source:
            if not root:
                raise SystemExit('--bundle or --root is required')
            bundle = _open_existing(root, args.replica_id).export_bundle()
        else:
            bundle = read_bundle(source)
        errors = validate_bundle(bundle)
        if errors:
            for e in errors:
                print(e)
            print(f'{len(errors)} error(s)')
            return 2
        print('valid')
        return 0
    if args.command == 'status':
        store = _open_existing(root, args.replica_id)
        p = store.projection()
        w = summarize_work(p)
        print(f"workspace {p['workspace']['id']} ({p['workspace']['title']}) revision {p['workspace']['revision']}")
        print(f"work items: {w['total']} total, {w['done']} done, {w['active']} active, {w['blocked']} blocked; tasks active: {w['tasks_active']}")
        print(f"sessions: {len(p['sessions'])}; leases live: {sum(1 for l in p['leases'] if l['live'])}; "
              f"gates open: {sum(1 for g in p['gates'] if g['status'] == 'open')}; events: {p['counts']['events']}")
        return 0
    if args.command == 'verify':
        store = _open_existing(root, args.replica_id)
        result = store.verify_chain(args.replica_id)
        print(json.dumps(result))
        return 0 if result['ok'] else 3
    return 1


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
