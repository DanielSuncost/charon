"""Skills tool — lightweight procedural memory + bundled capabilities.

Two sources are merged:
  - Bundled skills shipped with Charon at <repo>/skills/<name>/SKILL.md
    (read-only; e.g. manim-video). These give every Charon install a set of
    first-class capabilities out of the box.
  - User skills stored under .charon_state/skills/<name>/SKILL.md
    (mutable; created/edited by the agent). A user skill shadows a bundled
    skill of the same name.

Editing a bundled skill copies it into state first (copy-on-write), so the
shipped copy is never mutated.
"""
from __future__ import annotations

from pathlib import Path
import re
import shutil
from tools import ToolContext, ToolResult


SKILLS_TOOL_DEF = {
    'name': 'Skills',
    'description': (
        'List and use reusable procedural skills and bundled capabilities '
        '(e.g. manim-video for making videos). Actions: list, view, create, '
        'patch, edit, delete. Always Skills(list) then Skills(view <name>) '
        'before doing work a skill covers.'
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
    """Mutable, user-created skills under .charon_state/skills/."""
    root = (ctx.state_dir or (ctx.project_root / '.charon_state')) / 'skills'
    root.mkdir(parents=True, exist_ok=True)
    return root


def _bundled_root() -> Path:
    """Read-only skills shipped with Charon at <repo>/skills/.

    This file lives at apps/core-daemon/tools/skills_tool.py, so the repo
    root is three parents up from the tools/ directory.
    """
    return Path(__file__).resolve().parents[3] / 'skills'


def _validate_name(name: str) -> str:
    n = (name or '').strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', n):
        raise ValueError('invalid skill name (use [a-z0-9_-], max 64 chars)')
    return n


def _skill_file(root: Path, name: str) -> Path:
    return root / name / 'SKILL.md'


def _resolve_skill(ctx: ToolContext, name: str) -> tuple[Path | None, str]:
    """Return (SKILL.md path, source) for a skill, state shadowing bundled.

    source is 'state', 'bundled', or '' if not found.
    """
    state = _skill_file(_skills_root(ctx), name)
    if state.exists():
        return state, 'state'
    bundled = _skill_file(_bundled_root(), name)
    if bundled.exists():
        return bundled, 'bundled'
    return None, ''


def _list_dir(root: Path) -> dict[str, str]:
    """{name: first-line-description} for skills directly under root."""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        sk = d / 'SKILL.md'
        if d.is_dir() and sk.exists():
            lines = sk.read_text(encoding='utf-8', errors='ignore').splitlines()
            # Prefer the YAML `description:` field, else first non-empty line.
            desc = ''
            for ln in lines[:12]:
                m = re.match(r'\s*description:\s*["\']?(.+?)["\']?\s*$', ln)
                if m:
                    desc = m.group(1)
                    break
            if not desc:
                desc = next((ln for ln in lines if ln.strip() and not ln.startswith('---')), '')
            out[d.name] = desc[:120]
    return out


def _ensure_state_copy(ctx: ToolContext, name: str) -> Path:
    """Ensure a mutable state copy exists (copy-on-write from bundled)."""
    state_path = _skill_file(_skills_root(ctx), name)
    if state_path.exists():
        return state_path
    bundled_dir = _bundled_root() / name
    state_dir = _skills_root(ctx) / name
    if bundled_dir.is_dir():
        shutil.copytree(bundled_dir, state_dir, dirs_exist_ok=True)
    else:
        state_dir.mkdir(parents=True, exist_ok=True)
    return state_path


def execute_skills(params: dict, ctx: ToolContext) -> ToolResult:
    action = str(params.get('action') or '').strip().lower()

    try:
        if action == 'list':
            bundled = _list_dir(_bundled_root())
            state = _list_dir(_skills_root(ctx))
            names = sorted(set(bundled) | set(state))
            if not names:
                return ToolResult(content='No skills found.', details={'skills': []})
            skills = []
            lines = [f'Skills ({len(names)}):']
            for n in names:
                src = 'state' if n in state else 'bundled'
                desc = state.get(n) or bundled.get(n) or ''
                skills.append({'name': n, 'description': desc, 'source': src})
                tag = '' if src == 'state' else '  [bundled]'
                lines.append(f'- {n}: {desc}{tag}')
            lines.append('')
            lines.append('Use Skills(view <name>) to read a skill before using it.')
            return ToolResult(content='\n'.join(lines), details={'skills': skills})

        name = _validate_name(str(params.get('name') or '').strip())

        if action == 'view':
            path, source = _resolve_skill(ctx, name)
            if not path:
                return ToolResult(content=f'Skill not found: {name}', is_error=True)
            body = path.read_text(encoding='utf-8', errors='ignore')
            skill_dir = path.parent
            header = (
                f'# Skill: {name} (source: {source})\n'
                f'# Directory: {skill_dir}\n'
                f'# Read referenced files (e.g. references/*.md, scripts/*) from that directory with the Read tool.\n\n'
            )
            return ToolResult(content=header + body, details={'name': name, 'source': source, 'dir': str(skill_dir)})

        if action == 'create':
            content = str(params.get('content') or '')
            if not content.strip():
                return ToolResult(content='Error: content is required for create.', is_error=True)
            existing, source = _resolve_skill(ctx, name)
            if existing:
                return ToolResult(content=f'Error: skill already exists ({source}): {name}', is_error=True)
            path = _skill_file(_skills_root(ctx), name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
            return ToolResult(content=f'Skill created: {name}', details={'name': name, 'path': str(path)})

        if action == 'edit':
            content = str(params.get('content') or '')
            if not content.strip():
                return ToolResult(content='Error: content is required for edit.', is_error=True)
            _, source = _resolve_skill(ctx, name)
            if not source:
                return ToolResult(content=f'Skill not found: {name}', is_error=True)
            path = _ensure_state_copy(ctx, name)  # copy-on-write for bundled skills
            path.write_text(content, encoding='utf-8')
            note = ' (forked bundled skill into state)' if source == 'bundled' else ''
            return ToolResult(content=f'Skill updated: {name}{note}')

        if action == 'patch':
            old = str(params.get('old_string') or '')
            new = str(params.get('new_string') or '')
            replace_all = bool(params.get('replace_all', False))
            if not old:
                return ToolResult(content='Error: old_string required for patch.', is_error=True)
            src_path, source = _resolve_skill(ctx, name)
            if not source:
                return ToolResult(content=f'Skill not found: {name}', is_error=True)
            text = src_path.read_text(encoding='utf-8', errors='ignore')
            count = text.count(old)
            if count == 0:
                return ToolResult(content='Error: old_string not found.', is_error=True)
            if count > 1 and not replace_all:
                return ToolResult(content='Error: old_string not unique; set replace_all=true.', is_error=True)
            updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            path = _ensure_state_copy(ctx, name)  # copy-on-write for bundled skills
            path.write_text(updated, encoding='utf-8')
            note = ' (forked bundled skill into state)' if source == 'bundled' else ''
            return ToolResult(content=f'Skill patched: {name} ({count if replace_all else 1} replacement(s)){note}')

        if action == 'delete':
            state_path = _skill_file(_skills_root(ctx), name)
            if not state_path.exists():
                bundled = _skill_file(_bundled_root(), name)
                if bundled.exists():
                    return ToolResult(content=f'Error: {name} is a bundled skill and cannot be deleted.', is_error=True)
                return ToolResult(content=f'Skill not found: {name}', is_error=True)
            shutil.rmtree(state_path.parent, ignore_errors=True)
            return ToolResult(content=f'Skill deleted: {name}')

        return ToolResult(content=f'Unknown action: {action}', is_error=True)

    except Exception as e:
        return ToolResult(content=f'Skills tool error: {e}', is_error=True)
