#!/usr/bin/env python3
"""system-map validate — check a declared map against the code.

    python3 skills/system-map/validate.py --root <dir> [--map system-map.json] [--json]

Exit 1 on validation errors (overlaps, unknown deps, unresolved anchors, bad shape).
Unmapped files are gaps (reported, never errors). Same rules as Acheron's
`scripts/system-map.mjs validate`.
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
    p.add_argument('--json', action='store_true', help='print the structured result instead of text')
    a = p.parse_args(argv)
    root = os.path.abspath(a.root)
    try:
        m = mapcore.load_map(root, a.map)
    except Exception as e:  # noqa: BLE001
        print(f'cannot read map {a.map}: {e}', file=sys.stderr)
        return 1
    v = mapcore.validate_map(m, root=root)
    if a.json:
        print(json.dumps(v, indent=2))
    else:
        print(mapcore.format_validation(v))
    return 0 if v['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
