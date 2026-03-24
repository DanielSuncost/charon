"""Charon memory tools — UserModel and ProjectKnowledge.

These tools let agents write to the shared memory tiers:
- UserModel: permanent, shared across all agents and projects
- ProjectKnowledge: per-project, shared across agents on that project

Both are injected into the system prompt as frozen snapshots.
Mid-task writes update the backing store but don't change the running prompt.
The next task gets a fresh snapshot.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tools import ToolContext, ToolResult

# ── Injection scanning ──────────────────────────────────────────────

_THREAT_PATTERNS = [
    (r'ignore\s+(previous|all|above|prior)\s+instructions', 'prompt_injection'),
    (r'do\s+not\s+tell\s+the\s+user', 'deception_hide'),
    (r'system\s+prompt\s+override', 'sys_prompt_override'),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', 'disregard_rules'),
]

_INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
}

USER_MODEL_CHAR_LIMIT = 2000
PROJECT_KNOWLEDGE_CHAR_LIMIT = 3000
ENTRY_DELIMITER = '\n§\n'


def _scan_content(content: str) -> str | None:
    """Scan for injection. Returns error string if blocked, None if clean."""
    for char in _INVISIBLE_CHARS:
        if char in content:
            return f'Blocked: content contains invisible unicode U+{ord(char):04X} (possible injection).'
    for pattern, pid in _THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return f'Blocked: content matches threat pattern "{pid}". Memory entries are injected into the system prompt.'
    return None


# ── UserModel tool ──────────────────────────────────────────────────

USER_MODEL_TOOL_DEF = {
    'name': 'UserModel',
    'description': (
        'Read or write the shared user profile that persists across all sessions and agents. '
        'Actions: read, set, correct, add_intention, remove. '
        'Use "set" to save structured preferences (category + key + value). '
        'Use "correct" to record explicit user corrections (highest priority, never auto-deleted). '
        'Use "add_intention" to record what the user wants to accomplish in a project. '
        'Categories: style, coding, tooling, workflow, patterns. '
        'When the user corrects you or expresses a preference, save it immediately.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['read', 'set', 'correct', 'add_intention', 'remove'],
                'description': (
                    'read: show full profile. '
                    'set: set a field in a category (needs category, key, value). '
                    'correct: record a user correction (needs content). '
                    'add_intention: set project intention (needs project, content, priority). '
                    'remove: remove a correction by substring match (needs old_text).'
                ),
            },
            'category': {
                'type': 'string',
                'enum': ['style', 'coding', 'tooling', 'workflow', 'patterns'],
                'description': 'Category for "set" action.',
            },
            'key': {
                'type': 'string',
                'description': 'Field name within the category (e.g., "verbosity", "naming", "python").',
            },
            'value': {
                'type': 'string',
                'description': 'Value for "set" action.',
            },
            'content': {
                'type': 'string',
                'description': 'Correction text for "correct", or intent text for "add_intention".',
            },
            'project': {
                'type': 'string',
                'description': 'Project name for "add_intention".',
            },
            'priority': {
                'type': 'string',
                'enum': ['high', 'normal', 'low'],
                'description': 'Priority for "add_intention". Default: normal.',
            },
            'old_text': {
                'type': 'string',
                'description': 'Substring to match for "remove". Must uniquely identify one correction.',
            },
        },
        'required': ['action'],
    },
}


def _load_user_entries(state_dir: Path) -> list[str]:
    """Load user model entries from SQLite or JSON."""
    entries = []
    try:
        from store_adapter import get_db, user_model_get
        db = get_db(state_dir)
        model = user_model_get(db)
        for key, value in model.items():
            if isinstance(value, dict) and 'value' in value:
                entries.append(str(value['value']))
            elif isinstance(value, str):
                entries.append(value)
    except Exception:
        try:
            um_path = state_dir / 'user_model.json'
            if um_path.exists():
                model = json.loads(um_path.read_text())
                for v in model.get('preferences', {}).values():
                    if isinstance(v, dict) and v.get('value'):
                        entries.append(str(v['value']))
        except Exception:
            pass
    return entries


def _save_user_entries(state_dir: Path, entries: list[str]) -> None:
    """Save user model entries to SQLite and JSON."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    # Build preferences dict
    prefs = {}
    for i, entry in enumerate(entries):
        key = f'entry_{i}'
        prefs[key] = {'value': entry, 'updated_at': now}

    # Save to SQLite
    try:
        from store_adapter import get_db, user_model_set
        db = get_db(state_dir)
        # Clear old entries
        db.execute("DELETE FROM user_model")
        for key, value in prefs.items():
            user_model_set(db, key, value)
    except Exception:
        pass

    # Save to JSON
    try:
        um_path = state_dir / 'user_model.json'
        model = {}
        if um_path.exists():
            try:
                model = json.loads(um_path.read_text())
            except Exception:
                model = {}
        model['preferences'] = prefs
        model['updated_at'] = now
        um_path.parent.mkdir(parents=True, exist_ok=True)
        um_path.write_text(json.dumps(model, indent=2))
    except Exception:
        pass

    # Also write USER.md for human readability
    try:
        md_path = state_dir / 'USER.md'
        md_path.write_text(ENTRY_DELIMITER.join(entries) if entries else '(empty)')
    except Exception:
        pass


def _format_entries(entries: list[str], char_limit: int, label: str) -> str:
    """Format entries with usage info."""
    content = ENTRY_DELIMITER.join(entries) if entries else '(empty)'
    current = len(content)
    pct = int(current / char_limit * 100) if char_limit else 0
    return (
        f'{label}\n'
        f'Usage: {pct}% — {current:,}/{char_limit:,} chars\n'
        f'Entries: {len(entries)}\n\n'
        f'{content}'
    )


def execute_user_model(params: dict, ctx: ToolContext) -> ToolResult:
    """Execute the UserModel tool (structured)."""
    from user_model_structured import (
        load_structured, save_structured, render_for_prompt,
        set_field, add_correction, remove_correction, set_intention,
        total_chars, CHAR_LIMIT,
    )

    action = str(params.get('action', '')).strip().lower()

    if not ctx.state_dir:
        return ToolResult(content='Error: state_dir not available.', is_error=True)

    model = load_structured(ctx.state_dir)

    if action == 'read':
        return ToolResult(content=render_for_prompt(model))

    if action == 'set':
        category = str(params.get('category', '')).strip()
        key = str(params.get('key', '')).strip()
        value = str(params.get('value', '')).strip()
        if not category or not key or not value:
            return ToolResult(content='Error: set requires category, key, and value.', is_error=True)
        if category not in ('style', 'coding', 'tooling', 'workflow', 'patterns'):
            return ToolResult(content=f'Error: invalid category "{category}".', is_error=True)

        scan_err = _scan_content(value)
        if scan_err:
            return ToolResult(content=scan_err, is_error=True)

        set_field(model, category, key, value)

        # Budget check
        if total_chars(model) > CHAR_LIMIT:
            # Revert
            del model[category][key]
            return ToolResult(
                content=f'Error: Would exceed {CHAR_LIMIT:,} char limit. Remove entries first.',
                is_error=True,
            )

        save_structured(ctx.state_dir, model)
        return ToolResult(content=f'Set {category}.{key} = {value}\n\n' + render_for_prompt(model))

    if action == 'correct':
        content = str(params.get('content', '')).strip()
        if not content:
            return ToolResult(content='Error: content is required for correct.', is_error=True)

        scan_err = _scan_content(content)
        if scan_err:
            return ToolResult(content=scan_err, is_error=True)

        add_correction(model, content)

        if total_chars(model) > CHAR_LIMIT:
            model['corrections'].pop()
            return ToolResult(
                content=f'Error: Would exceed {CHAR_LIMIT:,} char limit. Remove entries first.',
                is_error=True,
            )

        save_structured(ctx.state_dir, model)
        return ToolResult(content=f'Correction saved: {content}\n\n' + render_for_prompt(model))

    if action == 'add_intention':
        project = str(params.get('project', '')).strip()
        content = str(params.get('content', '')).strip()
        priority = str(params.get('priority', 'normal')).strip()
        if not project or not content:
            return ToolResult(content='Error: project and content are required.', is_error=True)

        scan_err = _scan_content(content)
        if scan_err:
            return ToolResult(content=scan_err, is_error=True)

        set_intention(model, project, content, priority)

        if total_chars(model) > CHAR_LIMIT:
            model['intentions'].pop()
            return ToolResult(
                content=f'Error: Would exceed {CHAR_LIMIT:,} char limit.',
                is_error=True,
            )

        save_structured(ctx.state_dir, model)
        return ToolResult(content=f'Intention set for {project}.\n\n' + render_for_prompt(model))

    if action == 'remove':
        old_text = str(params.get('old_text', '')).strip()
        if not old_text:
            return ToolResult(content='Error: old_text is required for remove.', is_error=True)

        model, found = remove_correction(model, old_text)
        if not found:
            return ToolResult(content=f'Error: No correction matched "{old_text}".', is_error=True)

        save_structured(ctx.state_dir, model)
        return ToolResult(content=f'Correction removed.\n\n' + render_for_prompt(model))

    # Backward compat: 'add' still works as 'correct'
    if action == 'add':
        content = str(params.get('content', '')).strip()
        if not content:
            return ToolResult(content='Error: content is required.', is_error=True)
        scan_err = _scan_content(content)
        if scan_err:
            return ToolResult(content=scan_err, is_error=True)
        add_correction(model, content)
        if total_chars(model) > CHAR_LIMIT:
            model['corrections'].pop()
            return ToolResult(content=f'Error: Would exceed {CHAR_LIMIT:,} char limit.', is_error=True)
        save_structured(ctx.state_dir, model)
        return ToolResult(content=f'Entry added.\n\n' + render_for_prompt(model))

    # Backward compat: 'replace' works on corrections
    if action == 'replace':
        old_text = str(params.get('old_text', '')).strip()
        content = str(params.get('content', '')).strip()
        if not old_text or not content:
            return ToolResult(content='Error: old_text and content required.', is_error=True)
        scan_err = _scan_content(content)
        if scan_err:
            return ToolResult(content=scan_err, is_error=True)
        corrections = model.get('corrections', [])
        matches = [i for i, c in enumerate(corrections) if isinstance(c, str) and old_text in c]
        if not matches:
            return ToolResult(content=f'Error: No correction matched "{old_text}".', is_error=True)
        if len(matches) > 1:
            return ToolResult(content='Error: Multiple corrections matched.', is_error=True)
        corrections[matches[0]] = content
        save_structured(ctx.state_dir, model)
        return ToolResult(content=f'Entry replaced.\n\n' + render_for_prompt(model))

    return ToolResult(
        content=f'Error: Unknown action "{action}". Use: read, set, correct, add_intention, remove.',
        is_error=True,
    )


# ── ProjectKnowledge tool ───────────────────────────────────────────

PROJECT_KNOWLEDGE_TOOL_DEF = {
    'name': 'ProjectKnowledge',
    'description': (
        'Read or write shared project knowledge that persists across sessions. '
        'Use this to save architecture decisions, conventions, known issues, and build commands. '
        'Actions: read, add, replace, remove. '
        'Knowledge is shared with all agents working on the same project.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['read', 'add', 'replace', 'remove'],
                'description': 'Action to perform on project knowledge.',
            },
            'content': {
                'type': 'string',
                'description': 'Entry content (for add) or new content (for replace).',
            },
            'old_text': {
                'type': 'string',
                'description': 'Substring to match for replace/remove.',
            },
        },
        'required': ['action'],
    },
}


def _knowledge_path(project_root: Path) -> Path:
    """Path to project knowledge file."""
    return project_root / '.charon' / 'PROJECT_KNOWLEDGE.md'


def _load_knowledge_entries(project_root: Path) -> list[str]:
    """Load project knowledge entries from the knowledge file."""
    kp = _knowledge_path(project_root)
    if not kp.exists():
        return []
    try:
        raw = kp.read_text(encoding='utf-8').strip()
        if not raw or raw == '(empty)':
            return []
        return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
    except Exception:
        return []


def _save_knowledge_entries(project_root: Path, entries: list[str]) -> None:
    """Save project knowledge entries."""
    kp = _knowledge_path(project_root)
    kp.parent.mkdir(parents=True, exist_ok=True)
    kp.write_text(ENTRY_DELIMITER.join(entries) if entries else '(empty)', encoding='utf-8')


def execute_project_knowledge(params: dict, ctx: ToolContext) -> ToolResult:
    """Execute the ProjectKnowledge tool."""
    action = str(params.get('action', '')).strip().lower()
    content = str(params.get('content', '')).strip()
    old_text = str(params.get('old_text', '')).strip()

    entries = _load_knowledge_entries(ctx.project_root)

    if action == 'read':
        return ToolResult(content=_format_entries(
            entries, PROJECT_KNOWLEDGE_CHAR_LIMIT,
            f'Project Knowledge ({ctx.project_root.name})'))

    if action == 'add':
        if not content:
            return ToolResult(content='Error: content is required for add.', is_error=True)

        scan_err = _scan_content(content)
        if scan_err:
            return ToolResult(content=scan_err, is_error=True)

        if content in entries:
            return ToolResult(content='Entry already exists.\n\n'
                              + _format_entries(entries, PROJECT_KNOWLEDGE_CHAR_LIMIT,
                                                f'Project Knowledge ({ctx.project_root.name})'))

        test_entries = entries + [content]
        new_total = len(ENTRY_DELIMITER.join(test_entries))
        if new_total > PROJECT_KNOWLEDGE_CHAR_LIMIT:
            current = len(ENTRY_DELIMITER.join(entries)) if entries else 0
            return ToolResult(
                content=f'Error: Adding this entry ({len(content)} chars) would exceed the '
                        f'{PROJECT_KNOWLEDGE_CHAR_LIMIT:,} char limit. Current: {current:,}.',
                is_error=True,
            )

        entries.append(content)
        _save_knowledge_entries(ctx.project_root, entries)
        return ToolResult(content='Entry added.\n\n'
                          + _format_entries(entries, PROJECT_KNOWLEDGE_CHAR_LIMIT,
                                            f'Project Knowledge ({ctx.project_root.name})'))

    if action == 'replace':
        if not old_text:
            return ToolResult(content='Error: old_text is required for replace.', is_error=True)
        if not content:
            return ToolResult(content='Error: content is required for replace.', is_error=True)

        scan_err = _scan_content(content)
        if scan_err:
            return ToolResult(content=scan_err, is_error=True)

        matches = [(i, e) for i, e in enumerate(entries) if old_text in e]
        if not matches:
            return ToolResult(content=f'Error: No entry matched "{old_text}".', is_error=True)
        if len(matches) > 1:
            return ToolResult(
                content=f'Error: Multiple entries matched. Be more specific.',
                is_error=True,
            )

        idx = matches[0][0]
        test_entries = entries.copy()
        test_entries[idx] = content
        if len(ENTRY_DELIMITER.join(test_entries)) > PROJECT_KNOWLEDGE_CHAR_LIMIT:
            return ToolResult(content=f'Error: Replacement would exceed limit.', is_error=True)

        entries[idx] = content
        _save_knowledge_entries(ctx.project_root, entries)
        return ToolResult(content='Entry replaced.\n\n'
                          + _format_entries(entries, PROJECT_KNOWLEDGE_CHAR_LIMIT,
                                            f'Project Knowledge ({ctx.project_root.name})'))

    if action == 'remove':
        if not old_text:
            return ToolResult(content='Error: old_text is required for remove.', is_error=True)

        matches = [(i, e) for i, e in enumerate(entries) if old_text in e]
        if not matches:
            return ToolResult(content=f'Error: No entry matched "{old_text}".', is_error=True)
        if len(matches) > 1:
            return ToolResult(content=f'Error: Multiple entries matched. Be more specific.', is_error=True)

        entries.pop(matches[0][0])
        _save_knowledge_entries(ctx.project_root, entries)
        return ToolResult(content='Entry removed.\n\n'
                          + _format_entries(entries, PROJECT_KNOWLEDGE_CHAR_LIMIT,
                                            f'Project Knowledge ({ctx.project_root.name})'))

    return ToolResult(content=f'Error: Unknown action "{action}". Use: read, add, replace, remove.',
                      is_error=True)
