"""Structured user model store.

Seven categories:
  style       — communication preferences (verbosity, tone, etc.)
  coding      — coding conventions (naming, error handling, etc.)
  tooling     — tools and environment (python version, package manager, etc.)
  workflow    — workflow preferences (PR size, review process, etc.)
  corrections — explicit user corrections (never auto-deleted)
  intentions  — cross-project goals and priorities
  patterns    — observed interaction patterns (learned, not stated)

Storage: SQLite user_model table (key=category, value=JSON).
Export: .charon_state/USER.md (human-readable markdown).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


CATEGORIES = ('style', 'coding', 'tooling', 'workflow', 'corrections', 'intentions', 'patterns', 'interests', 'mental_model', 'ideas')
CHAR_LIMIT = 5000

_CATEGORY_LABELS = {
    'style': 'Style',
    'coding': 'Coding',
    'tooling': 'Tooling',
    'workflow': 'Workflow',
    'corrections': 'Corrections',
    'intentions': 'Intentions',
    'patterns': 'Patterns',
    'interests': 'Interests',
    'mental_model': 'Mental Model',
    'ideas': 'Ideas',
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Load / Save ─────────────────────────────────────────────────────

def _default_model() -> dict:
    return {cat: {} if cat not in ('corrections', 'intentions', 'ideas') else [] for cat in CATEGORIES}


def load_structured(state_dir: Path) -> dict:
    """Load the structured user model from SQLite, falling back to JSON."""
    model = _default_model()

    # Try SQLite
    try:
        from store_adapter import get_db, user_model_get
        db = get_db(state_dir)
        raw = user_model_get(db)
        for cat in CATEGORIES:
            if cat in raw:
                val = raw[cat]
                if isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except Exception:
                        pass
                if isinstance(val, dict) and 'value' in val:
                    val = val['value']
                    if isinstance(val, str):
                        try:
                            val = json.loads(val)
                        except Exception:
                            pass
                model[cat] = val
        # Load meta
        if '_meta' in raw:
            meta = raw['_meta']
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            if isinstance(meta, dict) and 'value' in meta:
                meta = meta['value']
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}
            model['_meta'] = meta
    except Exception:
        pass

    # Migrate flat entries if structured categories are empty
    if all(not model.get(c) for c in CATEGORIES):
        model = _migrate_flat_entries(state_dir, model)

    return model


def _migrate_flat_entries(state_dir: Path, model: dict) -> dict:
    """Migrate old flat user_model entries into structured categories."""
    try:
        from store_adapter import get_db, user_model_get
        db = get_db(state_dir)
        raw = user_model_get(db)
        flat_entries = []
        for key, value in raw.items():
            if key.startswith('entry_') or key in ('response_style', 'local_model'):
                if isinstance(value, dict) and 'value' in value:
                    flat_entries.append(str(value['value']))
                elif isinstance(value, str):
                    flat_entries.append(value)
        if flat_entries:
            # Put all flat entries into corrections as a safe default
            # (they'll get properly categorized on first consolidation)
            model['corrections'] = flat_entries
    except Exception:
        pass

    # Also try JSON file
    if not model.get('corrections'):
        try:
            um_path = state_dir / 'user_model.json'
            if um_path.exists():
                data = json.loads(um_path.read_text())
                prefs = data.get('preferences', {})
                flat = [str(v.get('value', '')) for v in prefs.values() if v.get('value')]
                if flat:
                    model['corrections'] = flat
        except Exception:
            pass

    return model


def save_structured(state_dir: Path, model: dict) -> None:
    """Save the structured user model to SQLite and USER.md."""
    # Save to SQLite
    try:
        from store_adapter import get_db, user_model_set
        db = get_db(state_dir)
        # Clear old entries
        db.execute("DELETE FROM user_model")
        db.commit()
        for cat in CATEGORIES:
            if model.get(cat):
                user_model_set(db, cat, model[cat])
        if model.get('_meta'):
            user_model_set(db, '_meta', model['_meta'])
    except Exception:
        pass

    # Export to USER.md
    try:
        md = render_markdown(model)
        md_path = state_dir / 'USER.md'
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding='utf-8')
    except Exception:
        pass

    # Also write JSON for backward compat
    try:
        um_path = state_dir / 'user_model.json'
        um_path.parent.mkdir(parents=True, exist_ok=True)
        export = {'version': 2, 'updated_at': _now()}
        for cat in CATEGORIES:
            export[cat] = model.get(cat, {} if cat != 'corrections' else [])
        um_path.write_text(json.dumps(export, indent=2, ensure_ascii=False))
    except Exception:
        pass


# ── Ideas ──────────────────────────────────────────────────────────

_CATEGORY_KEYWORDS = {
    'feature': ('add ', 'implement', 'enable', 'support', 'should be able to',
                'need a', 'want a', 'we need', 'button', 'panel', 'pane',
                'widget', 'command', 'shortcut', 'ui ', 'ux ', 'display'),
    'project': ('build ', 'create ', 'new project', 'start a', 'launch',
                'app ', 'application', 'service', 'platform', 'system'),
    'improvement': ('improve', 'optimize', 'faster', 'better', 'fix ',
                    'reduce', 'refactor', 'simplify', 'clean up', 'polish',
                    'performance', 'latency', 'lag', 'slow'),
}


def _auto_categorize(text: str) -> str:
    """Guess idea category from keywords. Returns 'feature', 'project', 'improvement', or 'general'."""
    lower = text.lower()
    scores: dict[str, int] = {}
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        scores[cat] = sum(1 for kw in keywords if kw in lower)
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    return best if scores[best] > 0 else 'general'


def record_idea(
    state_dir: Path,
    *,
    summary: str,
    session_id: str = '',
    message_seq: int = -1,
    message_text: str = '',
    category: str = '',
    source: str = 'explicit',
) -> dict:
    """Record an idea in the user model. Returns the new idea entry.

    If category is empty, auto-categorizes from the summary text.
    """
    import uuid
    if not category:
        category = _auto_categorize(summary)
    idea = {
        'id': f'idea-{uuid.uuid4().hex[:8]}',
        'summary': summary.strip()[:240],
        'session_id': session_id,
        'message_seq': message_seq,
        'message_text': message_text[:500],
        'category': category,
        'created_at': _now(),
        'source': source,
    }
    model = load_structured(state_dir)
    ideas = model.get('ideas', [])
    if not isinstance(ideas, list):
        ideas = []
    ideas.append(idea)
    model['ideas'] = ideas
    save_structured(state_dir, model)

    # Cross-link: also create a backlog goal if goal_runtime is available
    try:
        from goal_runtime import ingest_idea as _ingest_goal
        _ingest_goal(
            state_dir,
            agent_id=session_id or 'global',
            project='charon',
            text=summary,
            priority='normal',
        )
    except Exception:
        pass

    return idea


def list_ideas(state_dir: Path) -> list[dict]:
    """Return all recorded ideas, newest first."""
    model = load_structured(state_dir)
    ideas = model.get('ideas', [])
    if not isinstance(ideas, list):
        return []
    return list(reversed(ideas))


def lookup_idea_context(state_dir: Path, idea_id: str) -> dict | None:
    """Look up an idea and its originating conversation context."""
    model = load_structured(state_dir)
    ideas = model.get('ideas', [])
    if not isinstance(ideas, list):
        return None
    idea = next((i for i in ideas if i.get('id') == idea_id), None)
    if not idea:
        return None
    result = dict(idea)
    # Try to fetch surrounding messages from the conversation store
    session_id = idea.get('session_id', '')
    msg_seq = idea.get('message_seq', -1)
    if session_id and msg_seq >= 0:
        try:
            from context_store import ContextStore
            from store_adapter import get_db
            db = get_db(state_dir)
            messages = ContextStore.get_messages_for_agent(db, session_id)
            start = max(0, msg_seq - 2)
            end = min(len(messages), msg_seq + 3)
            result['context_messages'] = [
                {'role': m.role, 'content': m.content[:500], 'seq': i}
                for i, m in enumerate(messages[start:end], start=start)
            ]
        except Exception:
            result['context_messages'] = []
    return result


# ── Rendering ───────────────────────────────────────────────────────

def _render_dict_section(label: str, d: dict, include_keys: bool = False) -> str:
    """Render a dict category as a one-liner."""
    if not d:
        return ''
    if include_keys:
        parts = [f'{k}: {v}' for k, v in d.items() if v]
    else:
        parts = [str(v) for v in d.values() if v]
    if not parts:
        return ''
    return f'{label}: {", ".join(parts)}'


def _render_list_section(label: str, items: list) -> str:
    """Render a list category with bullet points."""
    if not items:
        return ''
    lines = [f'{label}:']
    for item in items:
        if isinstance(item, dict):
            # intentions format
            proj = item.get('project', '?')
            intent = item.get('intent', '')
            priority = item.get('priority', 'normal')
            lines.append(f'- {proj} ({priority}): {intent}')
        else:
            lines.append(f'- {item}')
    return '\n'.join(lines)


def render_for_prompt(model: dict) -> str:
    """Render the user model for system prompt injection.

    Returns the full block with ═══ delimiters, or empty string if no data.
    """
    sections = []

    for cat in ('style', 'coding', 'tooling', 'workflow'):
        data = model.get(cat)
        if isinstance(data, dict) and data:
            line = _render_dict_section(_CATEGORY_LABELS[cat], data)
            if line:
                sections.append(line)

    corrections = model.get('corrections')
    if isinstance(corrections, list) and corrections:
        sections.append(_render_list_section('Corrections', corrections))

    intentions = model.get('intentions')
    if isinstance(intentions, list) and intentions:
        sections.append(_render_list_section('Intentions', intentions))

    patterns = model.get('patterns')
    if isinstance(patterns, dict) and patterns:
        line = _render_dict_section('Patterns', patterns, include_keys=True)
        if line:
            sections.append(line)

    interests = model.get('interests')
    if isinstance(interests, dict) and interests:
        line = _render_dict_section('Interests', interests, include_keys=True)
        if line:
            sections.append(line)

    mental_model = model.get('mental_model')
    if isinstance(mental_model, dict) and mental_model:
        line = _render_dict_section('Mental Model', mental_model, include_keys=True)
        if line:
            sections.append(line)

    ideas = model.get('ideas')
    if isinstance(ideas, list) and ideas:
        lines = ['Ideas:']
        for i, idea in enumerate(ideas[-10:], 1):  # show last 10
            summary = idea.get('summary', '?') if isinstance(idea, dict) else str(idea)
            cat = idea.get('category', '') if isinstance(idea, dict) else ''
            tag = f' [{cat}]' if cat and cat != 'general' else ''
            lines.append(f'- #{i}{tag}: {summary}')
        sections.append('\n'.join(lines))

    if not sections:
        content = '(No profile yet. Save preferences with the UserModel tool.)'
    else:
        content = '\n'.join(sections)

    char_count = len(content)
    pct = int(char_count / CHAR_LIMIT * 100) if CHAR_LIMIT else 0
    sep = '═' * 46

    return (
        f'{sep}\n'
        f'USER PROFILE [{pct}% — {char_count:,}/{CHAR_LIMIT:,} chars]\n'
        f'{sep}\n'
        f'{content}\n'
        f'{sep}'
    )


def render_markdown(model: dict) -> str:
    """Render the user model as human-readable markdown for USER.md."""
    lines = ['# User Profile', '']

    for cat in ('style', 'coding', 'tooling', 'workflow'):
        data = model.get(cat)
        if isinstance(data, dict) and data:
            lines.append(f'## {_CATEGORY_LABELS[cat]}')
            for k, v in data.items():
                lines.append(f'- **{k}**: {v}')
            lines.append('')

    corrections = model.get('corrections')
    if isinstance(corrections, list) and corrections:
        lines.append('## Corrections')
        for c in corrections:
            if isinstance(c, str):
                lines.append(f'- {c}')
        lines.append('')

    intentions = model.get('intentions')
    if isinstance(intentions, list) and intentions:
        lines.append('## Intentions')
        for item in intentions:
            if isinstance(item, dict):
                proj = item.get('project', '?')
                intent = item.get('intent', '')
                priority = item.get('priority', 'normal')
                lines.append(f'- **{proj}** ({priority}): {intent}')
            else:
                lines.append(f'- {item}')
        lines.append('')

    patterns = model.get('patterns')
    if isinstance(patterns, dict) and patterns:
        lines.append('## Patterns')
        for k, v in patterns.items():
            lines.append(f'- **{k}**: {v}')
        lines.append('')

    interests = model.get('interests')
    if isinstance(interests, dict) and interests:
        lines.append('## Interests')
        for k, v in interests.items():
            lines.append(f'- **{k}**: {v}')
        lines.append('')

    mental_model = model.get('mental_model')
    if isinstance(mental_model, dict) and mental_model:
        lines.append('## Mental Model')
        for k, v in mental_model.items():
            lines.append(f'- **{k}**: {v}')
        lines.append('')

    ideas = model.get('ideas')
    if isinstance(ideas, list) and ideas:
        lines.append('## Ideas')
        for idea in ideas:
            if isinstance(idea, dict):
                summary = idea.get('summary', '?')
                cat = idea.get('category', '')
                src = idea.get('source', '')
                sid = idea.get('session_id', '')
                tag = f' [{cat}]' if cat and cat != 'general' else ''
                ref = f' (session:{sid})' if sid else ''
                lines.append(f'- {idea.get("id", "?")}{tag}: {summary} *{src}*{ref}')
            else:
                lines.append(f'- {idea}')
        lines.append('')

    if len(lines) <= 2:
        lines.append('(No profile yet.)')

    return '\n'.join(lines)


# ── Category operations ─────────────────────────────────────────────

def set_field(model: dict, category: str, key: str, value: str) -> dict:
    """Set a field in a dict category (style, coding, tooling, workflow, patterns, interests, mental_model)."""
    if category not in ('style', 'coding', 'tooling', 'workflow', 'patterns', 'interests', 'mental_model'):
        raise ValueError(f'Category {category} does not support set_field')
    if not isinstance(model.get(category), dict):
        model[category] = {}
    model[category][key] = value
    return model


def add_correction(model: dict, content: str) -> dict:
    """Add a correction. Never deduplicated — user's explicit voice."""
    if not isinstance(model.get('corrections'), list):
        model['corrections'] = []
    model['corrections'].append(content)
    return model


def remove_correction(model: dict, old_text: str) -> tuple[dict, bool]:
    """Remove a correction by substring match. Returns (model, found)."""
    corrections = model.get('corrections', [])
    if not isinstance(corrections, list):
        return model, False
    matches = [i for i, c in enumerate(corrections) if isinstance(c, str) and old_text in c]
    if len(matches) == 1:
        corrections.pop(matches[0])
        return model, True
    return model, False


def set_intention(model: dict, project: str, intent: str, priority: str = 'normal') -> dict:
    """Set or update a project intention."""
    if not isinstance(model.get('intentions'), list):
        model['intentions'] = []
    for item in model['intentions']:
        if isinstance(item, dict) and item.get('project') == project:
            item['intent'] = intent
            item['priority'] = priority
            item['last_updated'] = _now()[:10]
            return model
    model['intentions'].append({
        'project': project,
        'intent': intent,
        'priority': priority,
        'last_updated': _now()[:10],
    })
    return model


def total_chars(model: dict) -> int:
    """Calculate total rendered chars for budget checking."""
    rendered = render_for_prompt(model)
    # Subtract the delimiter lines and header
    lines = rendered.split('\n')
    content_lines = [ln for ln in lines if not ln.startswith('═') and not ln.startswith('USER PROFILE')]
    return len('\n'.join(content_lines).strip())
