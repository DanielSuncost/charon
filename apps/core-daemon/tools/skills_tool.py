"""Skills tool — lightweight procedural memory management.

Stores skills under .charon_state/skills/<name>/SKILL.md.
"""
from __future__ import annotations

from pathlib import Path
import re
from tools import ToolContext, ToolResult


SKILLS_TOOL_DEF = {
    'name': 'Skills',
    'description': (
        'Manage reusable procedural skills. Actions: list, view, create, patch, edit, delete.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': ['list', 'view', 'create', 'patch', 'edit', 'delete']},
            'name': {'type': 'string'},
            'content': {'type': 'string'},
            'old_string': {'type': 'string'},
            'new_string': {'type': 'string'},
            'replace_all': {'type': 'boolean'},
        },
        'required': ['action'],
    },
}


def _skills_root(ctx: ToolContext) -> Path:
    root = (ctx.state_dir or (ctx.project_root / '.charon_state')) / 'skills'
    root.mkdir(parents=True, exist_ok=True)
    return root


def _validate_name(name: str) -> str:
    n = (name or '').strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', n):
        raise ValueError('invalid skill name (use [a-z0-9_-], max 64 chars)')
    return n


def _skill_file(root: Path, name: str) -> Path:
    return root / name / 'SKILL.md'


def execute_skills(params: dict, ctx: ToolContext) -> ToolResult:
    action = str(params.get('action') or '').strip().lower()
    root = _skills_root(ctx)

    try:
        if action == 'list':
            skills = []
            for d in sorted(root.iterdir()) if root.exists() else []:
                if d.is_dir() and (d / 'SKILL.md').exists():
                    first = (d / 'SKILL.md').read_text(encoding='utf-8', errors='ignore').splitlines()
                    desc = first[0][:120] if first else ''
                    skills.append({'name': d.name, 'description': desc})
            if not skills:
                return ToolResult(content='No skills found.', details={'skills': []})
            lines = [f'Skills ({len(skills)}):']
            for s in skills:
                lines.append(f"- {s['name']}: {s['description']}")
            return ToolResult(content='\n'.join(lines), details={'skills': skills})

        name = _validate_name(str(params.get('name') or '').strip())
        path = _skill_file(root, name)

        if action == 'view':
            if not path.exists():
                return ToolResult(content=f'Skill not found: {name}', is_error=True)
            return ToolResult(content=path.read_text(encoding='utf-8', errors='ignore'))

        if action == 'create':
            content = str(params.get('content') or '')
            if not content.strip():
                return ToolResult(content='Error: content is required for create.', is_error=True)
            if path.exists():
                return ToolResult(content=f'Error: skill already exists: {name}', is_error=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
            return ToolResult(content=f'Skill created: {name}', details={'name': name, 'path': str(path)})

        if action == 'edit':
            content = str(params.get('content') or '')
            if not content.strip():
                return ToolResult(content='Error: content is required for edit.', is_error=True)
            if not path.exists():
                return ToolResult(content=f'Skill not found: {name}', is_error=True)
            path.write_text(content, encoding='utf-8')
            return ToolResult(content=f'Skill updated: {name}')

        if action == 'patch':
            old = str(params.get('old_string') or '')
            new = str(params.get('new_string') or '')
            replace_all = bool(params.get('replace_all', False))
            if not old:
                return ToolResult(content='Error: old_string required for patch.', is_error=True)
            if not path.exists():
                return ToolResult(content=f'Skill not found: {name}', is_error=True)
            text = path.read_text(encoding='utf-8', errors='ignore')
            count = text.count(old)
            if count == 0:
                return ToolResult(content='Error: old_string not found.', is_error=True)
            if count > 1 and not replace_all:
                return ToolResult(content='Error: old_string not unique; set replace_all=true.', is_error=True)
            updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            path.write_text(updated, encoding='utf-8')
            return ToolResult(content=f'Skill patched: {name} ({count if replace_all else 1} replacement(s))')

        if action == 'delete':
            if not path.exists():
                return ToolResult(content=f'Skill not found: {name}', is_error=True)
            path.unlink()
            try:
                path.parent.rmdir()
            except Exception:
                pass
            return ToolResult(content=f'Skill deleted: {name}')

        return ToolResult(content=f'Unknown action: {action}', is_error=True)

    except Exception as e:
        return ToolResult(content=f'Skills tool error: {e}', is_error=True)
