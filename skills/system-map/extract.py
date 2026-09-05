#!/usr/bin/env python3
"""system-map extract — derived facts from the code for a declared map.

    python3 skills/system-map/extract.py --root <dir> [--map system-map.json] [--out derived.json] [--no-git]

Prints (or writes) the derived-facts JSON: per-component files/loc/tests/last_change,
import relations with evidence, gaps (unmapped files) and conflicts
(undeclared_dependency / dead_dependency). Same contract as Acheron's
`scripts/system-map.mjs extract`. Exit 1 when the map does not validate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mapcore  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--root', default='.')
    p.add_argument('--map', default=mapcore.DEFAULT_MAP_FILE)
    p.add_argument('--out', default=None)
    p.add_argument('--no-git', action='store_true')
    a = p.parse_args(argv)
    root = os.path.abspath(a.root)
    try:
        m = mapcore.load_map(root, a.map)
    except Exception as e:  # noqa: BLE001
        print(f'cannot read map {a.map}: {e}', file=sys.stderr)
        return 1
    d = mapcore.extract_facts(m, root=root, git=not a.no_git)
    text = json.dumps(d, indent=2, ensure_ascii=False)
    if a.out:
        with open(os.path.abspath(a.out), 'w', encoding='utf-8') as fh:
            fh.write(text + '\n')
        print(f'wrote {a.out}: {len(d["relations"])} relations, {len(d["gaps"])} gaps, {len(d["conflicts"])} conflicts')
    else:
        print(text)
    return 0 if d['validation']['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
