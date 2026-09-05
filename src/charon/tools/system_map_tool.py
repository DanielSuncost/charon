"""SystemMap tool — the system-map skill (skills/system-map/) as a Charon tool.

Runs the skill in-process by loading its stdlib-only modules by path, so any
Charon agent (not just the overseer) can validate a declared map, extract derived
facts, or query the merged view. Same contract as Acheron's map core and its
`acheron_map_get` / `acheron_map_refresh` tools.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from charon.tools import ToolContext, ToolResult
from charon.workspace.system_map_skill import load_skill_module

SYSTEM_MAP_TOOL_DEF = {
    'name': 'SystemMap',
    'description': (
        'The project\'s system map: a declared system-map.json (subsystems → components → code) plus '
        'facts derived from the code. action=validate checks the declaration against the code (errors, '
        'warnings, gaps, coverage, anchors); action=extract derives facts (per-component size/tests/last '
        'change, import relations with file:line evidence, gaps, undeclared/dead dependency conflicts); '
        'action=query returns the merged declared+derived view of one component (or the table). '
        'Declared beats inferred: report disagreements, never edit the map silently.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': ['validate', 'extract', 'query'], 'description': 'What to do'},
            'root': {'type': 'string', 'description': 'Project root holding system-map.json (default: the tool context project root)'},
            'map': {'type': 'string', 'description': 'Map file relative to root (default system-map.json)'},
            'component': {'type': 'string', 'description': 'query: component id; omit for the per-component table'},
            'out': {'type': 'string', 'description': 'extract: write derived JSON to this path (relative to root or absolute)'},
            'derived': {'type': 'string', 'description': 'query: reuse a previously extracted derived.json instead of extracting now'},
            'no_git': {'type': 'boolean', 'description': 'Skip git (source_revision / last_change become null)'},
        },
        'required': ['action'],
    },
}


def _root(params: dict, ctx: ToolContext) -> Path:
    raw = params.get('root') or (ctx.metadata or {}).get('map_root') if ctx else params.get('root')
    if raw:
        p = Path(str(raw)).expanduser()
        if not p.is_absolute():
            p = Path(ctx.project_root) / p
    else:
        p = Path(ctx.project_root)
    return p.resolve()


def execute_system_map(params: dict, ctx: ToolContext) -> ToolResult:
    action = str(params.get('action') or '').strip()
    if action not in ('validate', 'extract', 'query'):
        return ToolResult(content='action must be validate, extract, or query', is_error=True)
    try:
        core = load_skill_module('mapcore')
    except Exception as e:  # noqa: BLE001
        return ToolResult(content=f'system-map skill unavailable: {e}', is_error=True)
    root = _root(params, ctx)
    if not root.is_dir():
        return ToolResult(content=f'root is not a directory: {root}', is_error=True)
    map_file = str(params.get('map') or core.DEFAULT_MAP_FILE)
    try:
        m = core.load_map(str(root), map_file)
    except FileNotFoundError:
        return ToolResult(content=f'no {map_file} under {root} — declare one first (see skills/system-map/SKILL.md)', is_error=True)
    except Exception as e:  # noqa: BLE001
        return ToolResult(content=f'cannot read {map_file}: {e}', is_error=True)
    use_git = not bool(params.get('no_git'))

    try:
        if action == 'validate':
            v = core.validate_map(m, root=str(root))
            result: dict[str, Any] = {'root': str(root), 'map': map_file, **v}
            text = core.format_validation(v)
            return ToolResult(content=text + '\n' + json.dumps({k: v[k] for k in ('ok', 'coverage')}), details=result)
        if action == 'extract':
            d = core.extract_facts(m, root=str(root), git=use_git)
            if params.get('out'):
                out = Path(str(params['out']))
                if not out.is_absolute():
                    out = root / out
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
                d = {**d, 'written_to': str(out)}
            return ToolResult(content=json.dumps(d, ensure_ascii=False), details=d)
        # query
        if params.get('derived'):
            dp = Path(str(params['derived']))
            if not dp.is_absolute():
                dp = root / dp
            derived = json.loads(dp.read_text(encoding='utf-8'))
        else:
            derived = core.extract_facts(m, root=str(root), git=use_git)
        comp = params.get('component')
        if comp:
            view = core.query_component(m, derived, str(comp))
            if view is None:
                known = ', '.join(c['id'] for c in m['components'])
                return ToolResult(content=f'unknown component "{comp}"; known: {known}', is_error=True)
            return ToolResult(content=json.dumps(view, ensure_ascii=False), details=view)
        rows = core.report_table(m, derived)
        table = {'rows': rows, 'relations': len(derived['relations']), 'gaps': len(derived['gaps']), 'conflicts': derived['conflicts'],
                 'freshness': {'extracted_at': derived.get('extracted_at'), 'source_revision': derived.get('source_revision')}}
        return ToolResult(content=core.format_table(rows, derived), details=table)
    except Exception as e:  # noqa: BLE001
        return ToolResult(content=f'system-map {action} failed: {type(e).__name__}: {e}', is_error=True)


SYSTEM_MAP_TOOL_DEFS = [SYSTEM_MAP_TOOL_DEF]
SYSTEM_MAP_TOOL_EXECUTORS = {'SystemMap': execute_system_map}

__all__ = ['SYSTEM_MAP_TOOL_DEF', 'SYSTEM_MAP_TOOL_DEFS', 'SYSTEM_MAP_TOOL_EXECUTORS', 'execute_system_map']

# keep `os` referenced for tools that monkeypatch environment in tests
_ = os
