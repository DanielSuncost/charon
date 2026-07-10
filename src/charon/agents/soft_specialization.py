"""Soft specialization — auto-derive a short role label from recent work.

Reads an agent's working memory (last N task summaries) and produces a
1-3 word label like "auth refactor", "TUI layout", "shade lifecycle".

Two modes:
1. heuristic: keyword extraction from task summaries (no LLM, instant)
2. llm: single cheap LLM call for a natural-language label

The label is written to agent.specialization and shows up in:
- system prompt (Layer 1 identity)
- coordination awareness (Layer 6, other agents see it)
- charon status / TUI
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any
from charon.infra import config

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# How many recent task summaries to consider
_WINDOW_SIZE = config.spec_window()

# Minimum tasks before generating a label (avoid noise from 1-2 tasks)
_MIN_TASKS = config.spec_min_tasks()

# How often to re-derive (seconds). Checked in the loop.
REFRESH_INTERVAL_SEC = config.spec_interval()

# Staleness: if no new tasks in this many seconds, keep current label
_STALE_THRESHOLD_SEC = 3600


# ---------------------------------------------------------------------------
# Heuristic mode (no LLM)
# ---------------------------------------------------------------------------

# Keywords → topic mapping. Order matters: first match wins for ties.
_TOPIC_KEYWORDS: list[tuple[str, list[str]]] = [
    ('auth',          ['auth', 'login', 'token', 'credential', 'oauth', 'refresh_token', 'jwt']),
    ('TUI',           ['tui', 'textual', 'layout', 'widget', 'terminal ui', 'ui_layout', 'ui_events']),
    ('frontend',      ['frontend', 'react', 'css', 'component', 'html', 'tailwind', 'jsx', 'tsx']),
    ('backend',       ['backend', 'api', 'endpoint', 'route', 'handler', 'server', 'fastapi', 'flask']),
    ('database',      ['database', 'sqlite', 'sql', 'migration', 'schema', 'db', 'postgres', 'store_adapter']),
    ('testing',       ['test', 'pytest', 'unittest', 'coverage', 'assertion', 'mock', 'fixture']),
    ('shade',         ['shade', 'contract', 'phase', 'orchestrat', 'spawn', 'ephemeral']),
    ('agent',         ['agent', 'lifecycle', 'runtime', 'working_memory', 'inbox']),
    ('prompt',        ['prompt', 'system_prompt', 'identity', 'layer', 'context']),
    ('memory',        ['memory', 'consolidat', 'user_model', 'knowledge', 'extractor']),
    ('git',           ['git', 'commit', 'branch', 'merge', 'rebase', 'diff']),
    ('docs',          ['doc', 'readme', 'markdown', 'documentation', 'comment']),
    ('refactor',      ['refactor', 'cleanup', 'reorganiz', 'restructur', 'rename']),
    ('build',         ['build', 'deploy', 'ci', 'docker', 'package', 'pip', 'npm']),
    ('config',        ['config', 'setting', 'onboarding', 'env', 'registry']),
    ('goals',         ['goal', 'objective', 'intention', 'backlog', 'roadmap']),
    ('conversation',  ['conversation', 'chat', 'message', 'steering', 'session']),
    ('tools',         ['tool', 'browser', 'http', 'web_tool', 'search_tool', 'bash']),
]

# Words to strip from summaries before matching
_STOP_WORDS = frozenset([
    'the', 'a', 'an', 'and', 'or', 'to', 'in', 'of', 'for', 'is', 'was',
    'with', 'on', 'at', 'by', 'from', 'it', 'this', 'that', 'not', 'no',
    'completed', 'ran', 'read', 'wrote', 'edited', 'file', 'files', 'command',
    'commands', 'error', 'ok', 'done', 'task', 'turn', 'turns',
])


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alnum, remove stop words."""
    words = re.findall(r'[a-z0-9_]+', text.lower())
    return [w for w in words if w not in _STOP_WORDS and len(w) > 1]


def _score_topics(summaries: list[str]) -> list[tuple[str, int]]:
    """Score topics by keyword frequency across summaries."""
    combined = ' '.join(summaries).lower()
    tokens = set(_tokenize(combined))
    # Also match substrings for partial matches like "orchestrat" in "orchestrator"
    scores: Counter = Counter()
    for topic, keywords in _TOPIC_KEYWORDS:
        for kw in keywords:
            if kw in combined:
                scores[topic] += 2
            if kw in tokens:
                scores[topic] += 1
    return scores.most_common()


def derive_label_heuristic(summaries: list[str]) -> str:
    """Derive a specialization label from task summaries without LLM.

    Returns a short phrase like "shade orchestration", "auth flow", "testing".
    Returns '' if not enough signal.
    """
    if len(summaries) < _MIN_TASKS:
        return ''

    recent = summaries[-_WINDOW_SIZE:]
    scored = _score_topics(recent)

    if not scored:
        return ''

    top_topic, top_score = scored[0]

    # If there's a clear secondary topic, combine them
    if len(scored) >= 2:
        second_topic, second_score = scored[1]
        # Only combine if secondary is close in score (within 40%)
        if second_score >= top_score * 0.6:
            return f'{top_topic} & {second_topic}'

    return top_topic


# ---------------------------------------------------------------------------
# LLM mode
# ---------------------------------------------------------------------------

_LLM_PROMPT = """Based on these recent task summaries for a coding agent, generate a 1-3 word label describing the agent's current area of focus. Output ONLY the label, nothing else.

Examples of good labels: "auth refactor", "TUI layout", "shade lifecycle", "database migration", "frontend styling", "test infrastructure"

Recent task summaries:
{summaries}

Label:"""


async def derive_label_llm(summaries: list[str], *, provider: Any, model: Any) -> str:
    """Derive a specialization label using a cheap LLM call.

    Falls back to heuristic if the call fails.
    """
    if len(summaries) < _MIN_TASKS:
        return ''

    recent = summaries[-_WINDOW_SIZE:]
    formatted = '\n'.join(f'- {s[:150]}' for s in recent)
    prompt = _LLM_PROMPT.format(summaries=formatted)

    text_parts = []
    try:
        async for delta in provider.stream(
            messages=[{'role': 'user', 'content': prompt}],
            model=model,
            system_prompt='Output only the label. 1-3 words. No explanation.',
            max_tokens=20,
        ):
            if hasattr(delta, 'type') and delta.type == 'text':
                text_parts.append(delta.text)
    except Exception as e:
        _diag('soft_specialization', 'LLM label call failed; using heuristic specialization label', error=e)
        return derive_label_heuristic(summaries)

    label = ''.join(text_parts).strip().strip('"\'').lower()
    # Sanitize: only keep short labels
    if label and len(label) <= 40 and '\n' not in label:
        return label
    return derive_label_heuristic(summaries)


# ---------------------------------------------------------------------------
# Integration: read memory, derive label, write to agent
# ---------------------------------------------------------------------------

def _get_summaries(state_dir: Path, agent_id: str) -> list[str]:
    """Read task summaries from working memory."""
    memory = None

    # Try SQLite
    try:
        from charon.infra.store_adapter import get_db, agent_memory_get
        db = get_db(state_dir)
        memory = agent_memory_get(db, agent_id)
    except Exception as e:
        _diag('soft_specialization', 'SQLite working-memory read failed; trying JSON fallback', error=e, agent_id=agent_id)

    # Fallback to JSON
    if not memory:
        try:
            mem_path = state_dir / 'agents' / agent_id / 'working_memory.json'
            if mem_path.exists():
                memory = json.loads(mem_path.read_text())
        except Exception as e:
            _diag('soft_specialization', 'working_memory.json unreadable; no summaries for labeling', error=e, agent_id=agent_id)

    if not memory:
        return []

    notes = memory.get('notes') or []
    return [str(n.get('summary', '')).strip() for n in notes if n.get('summary')]


def _get_current_specialization(state_dir: Path, agent_id: str) -> str:
    """Read current specialization from agent record."""
    try:
        from charon.infra.store_adapter import get_db, agent_get
        db = get_db(state_dir)
        agent = agent_get(db, agent_id)
        if agent:
            return agent.get('specialization', '')
    except Exception as e:
        _diag('soft_specialization', 'current-specialization read failed; assuming empty label', error=e, agent_id=agent_id)
    return ''


def _is_locked(state_dir: Path, agent_id: str) -> bool:
    """True when the specialization was assigned by the user (a long-lived
    specialist like a release engineer) — auto-derived labels must not
    overwrite it."""
    try:
        from charon.infra.store_adapter import get_db, agent_get
        db = get_db(state_dir)
        agent = agent_get(db, agent_id)
        if agent and agent.get('specialization_locked'):
            return True
    except Exception as e:
        _diag('soft_specialization', 'SQLite lock-flag read failed; checking agents.json fallback', error=e, agent_id=agent_id)
    # agents.json fallback (store may be disabled)
    try:
        agents_file = state_dir / 'agents.json'
        if agents_file.exists():
            for a in json.loads(agents_file.read_text()):
                if a.get('id') == agent_id:
                    return bool(a.get('specialization_locked'))
    except Exception as e:
        _diag('soft_specialization', 'agents.json lock-flag read failed; treating specialization as unlocked', error=e, agent_id=agent_id)
    return False


def _set_specialization(state_dir: Path, agent_id: str, label: str) -> None:
    """Write specialization to agent record (via extra JSON column)."""
    try:
        from charon.infra.store_adapter import get_db, agent_update
        db = get_db(state_dir)
        agent_update(db, agent_id, specialization=label)
    except Exception as e:
        _diag('soft_specialization', 'specialization write to SQLite failed; store misses new label', error=e, agent_id=agent_id)

    # Also update agents.json for backward compat
    try:
        agents_file = state_dir / 'agents.json'
        if agents_file.exists():
            agents = json.loads(agents_file.read_text())
            for a in agents:
                if a.get('id') == agent_id:
                    a['specialization'] = label
                    break
            agents_file.write_text(json.dumps(agents, indent=2))
    except Exception as e:
        _diag('soft_specialization', 'specialization write to agents.json failed; JSON misses new label', error=e, agent_id=agent_id)


# Track last refresh time per agent
_last_refresh: dict[str, float] = {}


def should_refresh(agent_id: str) -> bool:
    """Check if enough time has passed since last refresh."""
    last = _last_refresh.get(agent_id, 0)
    return (time.time() - last) >= REFRESH_INTERVAL_SEC


def refresh_specialization(
    state_dir: Path,
    agent_id: str,
    *,
    mode: str = 'heuristic',
    provider: Any = None,
    model: Any = None,
) -> str | None:
    """Derive and update specialization for an agent.

    Returns the new label, or None if skipped.
    """
    if not should_refresh(agent_id):
        return None

    if _is_locked(state_dir, agent_id):
        _last_refresh[agent_id] = time.time()
        return None

    summaries = _get_summaries(state_dir, agent_id)
    if len(summaries) < _MIN_TASKS:
        _last_refresh[agent_id] = time.time()
        return None

    # Derive label
    if mode == 'llm' and provider and model:
        import asyncio
        try:
            label = asyncio.run(derive_label_llm(summaries, provider=provider, model=model))
        except Exception as e:
            _diag('soft_specialization', 'LLM label refresh failed; using heuristic specialization label', error=e, agent_id=agent_id)
            label = derive_label_heuristic(summaries)
    else:
        label = derive_label_heuristic(summaries)

    if not label:
        _last_refresh[agent_id] = time.time()
        return None

    # Only update if changed
    current = _get_current_specialization(state_dir, agent_id)
    if label != current:
        _set_specialization(state_dir, agent_id, label)

    _last_refresh[agent_id] = time.time()
    return label


def refresh_all_agents(state_dir: Path, *, mode: str = 'heuristic') -> dict[str, str]:
    """Refresh specialization for all running non-shade agents.

    Returns {agent_id: new_label} for agents that were updated.
    """
    results = {}
    try:
        from charon.infra.store_adapter import get_db, agent_list
        db = get_db(state_dir)
        agents = agent_list(db)
    except Exception as e:
        _diag('soft_specialization', 'SQLite agent list failed; falling back to agents.json', error=e)
        # Fallback to JSON
        try:
            agents_file = state_dir / 'agents.json'
            agents = json.loads(agents_file.read_text()) if agents_file.exists() else []
        except Exception as exc:
            _diag('soft_specialization', 'agents.json unreadable; refreshing no agents', error=exc)
            agents = []

    for agent in agents:
        if agent.get('role') == 'shade':
            continue
        if agent.get('status') != 'running':
            continue
        agent_id = agent.get('id', '')
        if not agent_id:
            continue

        label = refresh_specialization(state_dir, agent_id, mode=mode)
        if label:
            results[agent_id] = label

    return results
