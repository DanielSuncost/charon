#!/usr/bin/env python3
"""system-map query — the merged declared + derived view of the system.

    python3 skills/system-map/query.py --root <dir> [--map system-map.json] [--derived derived.json] [--json] [component]

With a component id: its description, code locators, interfaces, invariants, declared
vs derived dependencies (undeclared / unobserved called out), dependents, size facts,
conflicts, and freshness (when the facts were extracted, at which revision). Without one:
the per-component table. If no --derived file is given the facts are extracted now.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mapcore  # noqa: E402


def _print_component(view: dict) -> None:
    print(f'{view["id"]} — {view["name"]}  [{view["subsystem"]} / {view["kind"]}]')
    if view['description']:
        print(f'  {view["description"]}')
    print(f'  code: {", ".join(view["code"]) or "-"}')
    if view['entrypoints']:
        print(f'  entrypoints: {", ".join(view["entrypoints"])}')
    for it in view['interfaces']:
        print(f'  interface: {it.get("kind")} {it.get("locator")}' + (f' ({it["protocol"]})' if it.get('protocol') else ''))
    for inv in view['invariants']:
        print(f'  invariant: {inv}')
    d = view['depends_on']
    print(f'  depends_on (declared): {", ".join(d["declared"]) or "-"}')
    print(f'  depends_on (derived):  {", ".join(d["derived"]) or "-"}')
    if d['undeclared']:
        print(f'  UNDECLARED (seen in code, not declared): {", ".join(d["undeclared"])}')
    if d['unobserved']:
        print(f'  unobserved (declared, no import evidence): {", ".join(d["unobserved"])}')
    if view['depended_on_by']:
        print(f'  depended on by: {", ".join(view["depended_on_by"])}')
    f = view['facts']
    lc = f.get('last_change')
    print(f'  facts: {f["files"]} files, {f["loc"]} loc, {f["tests"]} test files, languages {", ".join(f.get("languages") or []) or "-"}'
          + (f', last change {lc["at"][:10]} by {lc["by"]}' if lc else ''))
    fr = view['freshness']
    print(f'  freshness: extracted {fr.get("extracted_at") or "-"} at revision {(fr.get("source_revision") or "-")[:8]}')
    print(f'  provenance: declared = {view["provenance"]["declared"]}, derived = {view["provenance"]["derived"]}')


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('component', nargs='?')
    p.add_argument('--root', default='.')
    p.add_argument('--map', default=mapcore.DEFAULT_MAP_FILE)
    p.add_argument('--derived', default=None, help='previously extracted derived.json (default: extract now)')
    p.add_argument('--no-git', action='store_true')
    p.add_argument('--json', action='store_true')
    a = p.parse_args(argv)
    root = os.path.abspath(a.root)
    try:
        m = mapcore.load_map(root, a.map)
    except Exception as e:  # noqa: BLE001
        print(f'cannot read map {a.map}: {e}', file=sys.stderr)
        return 1
    if a.derived:
        with open(os.path.abspath(a.derived), 'r', encoding='utf-8') as fh:
            derived = json.load(fh)
    else:
        derived = mapcore.extract_facts(m, root=root, git=not a.no_git)
    if a.component:
        view = mapcore.query_component(m, derived, a.component)
        if view is None:
            print(f'unknown component "{a.component}"; known: {", ".join(c["id"] for c in m["components"])}', file=sys.stderr)
            return 1
        print(json.dumps(view, indent=2, ensure_ascii=False) if a.json else '')
        if not a.json:
            _print_component(view)
        return 0
    rows = mapcore.report_table(m, derived)
    print(json.dumps({'rows': rows, 'relations': len(derived['relations']), 'gaps': len(derived['gaps']), 'conflicts': derived['conflicts']}, indent=2) if a.json
          else mapcore.format_table(rows, derived))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
