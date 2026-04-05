"""Context transfer for provider/session handoff.

Creates a high-fidelity transfer bundle from:
- recent normalized message history
- execution memory (tool events + semantic recall)
- working memory / project knowledge snapshots
- git/workspace state

This is used when switching providers and choosing "continue session".
"""
from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from conversation_store import message_to_dict
from providers import Message


TRANSFER_PROFILES: dict[str, dict[str, Any]] = {
    'default': {
        'profile_name': 'default-standard',
        'max_context_tokens': 65536,
        'safe_prompt_fraction': 0.28,
        'preferred_style': 'hybrid',
        'supports_history_replay': True,
        'supports_tool_result_replay': 'weak',
        'message_mode': 'assistant_user_only',
        'tool_history_mode': 'flattened',
        'max_history_messages': 8,
        'max_execution_events': 6,
        'max_task_episodes': 3,
        'max_semantic_memories': 3,
        'max_working_memory_chars': 3000,
        'max_project_knowledge_chars': 2500,
        'max_transfer_block_chars': 14000,
    },
    'claude-code': {
        'profile_name': 'claude-large',
        'max_context_tokens': 200000,
        'safe_prompt_fraction': 0.42,
        'preferred_style': 'hybrid',
        'supports_history_replay': True,
        'supports_tool_result_replay': 'medium',
        'message_mode': 'assistant_user_only',
        'tool_history_mode': 'flattened',
        'max_history_messages': 16,
        'max_execution_events': 10,
        'max_task_episodes': 4,
        'max_semantic_memories': 4,
        'max_working_memory_chars': 8000,
        'max_project_knowledge_chars': 7000,
        'max_transfer_block_chars': 30000,
    },
    'anthropic': {
        'profile_name': 'claude-large',
        'max_context_tokens': 200000,
        'safe_prompt_fraction': 0.42,
        'preferred_style': 'hybrid',
        'supports_history_replay': True,
        'supports_tool_result_replay': 'medium',
        'message_mode': 'assistant_user_only',
        'tool_history_mode': 'flattened',
        'max_history_messages': 16,
        'max_execution_events': 10,
        'max_task_episodes': 4,
        'max_semantic_memories': 4,
        'max_working_memory_chars': 8000,
        'max_project_knowledge_chars': 7000,
        'max_transfer_block_chars': 30000,
    },
    'codex': {
        'profile_name': 'codex-large',
        'max_context_tokens': 200000,
        'safe_prompt_fraction': 0.38,
        'preferred_style': 'hybrid',
        'supports_history_replay': True,
        'supports_tool_result_replay': 'medium',
        'message_mode': 'assistant_user_only',
        'tool_history_mode': 'flattened',
        'max_history_messages': 14,
        'max_execution_events': 10,
        'max_task_episodes': 4,
        'max_semantic_memories': 4,
        'max_working_memory_chars': 6500,
        'max_project_knowledge_chars': 5000,
        'max_transfer_block_chars': 24000,
    },
    'openai': {
        'profile_name': 'openai-standard',
        'max_context_tokens': 128000,
        'safe_prompt_fraction': 0.33,
        'preferred_style': 'hybrid',
        'supports_history_replay': True,
        'supports_tool_result_replay': 'weak',
        'message_mode': 'assistant_user_only',
        'tool_history_mode': 'flattened',
        'max_history_messages': 10,
        'max_execution_events': 8,
        'max_task_episodes': 3,
        'max_semantic_memories': 3,
        'max_working_memory_chars': 5000,
        'max_project_knowledge_chars': 4000,
        'max_transfer_block_chars': 18000,
    },
    'openlm': {
        'profile_name': 'openlm-64k',
        'max_context_tokens': 64000,
        'safe_prompt_fraction': 0.24,
        'preferred_style': 'summary_first',
        'supports_history_replay': False,
        'supports_tool_result_replay': 'weak',
        'message_mode': 'none',
        'tool_history_mode': 'summary_only',
        'max_history_messages': 4,
        'max_execution_events': 5,
        'max_task_episodes': 2,
        'max_semantic_memories': 2,
        'max_working_memory_chars': 2200,
        'max_project_knowledge_chars': 1800,
        'max_transfer_block_chars': 9000,
    },
    'local': {
        'profile_name': 'local-64k',
        'max_context_tokens': 65536,
        'safe_prompt_fraction': 0.24,
        'preferred_style': 'summary_first',
        'supports_history_replay': False,
        'supports_tool_result_replay': 'weak',
        'message_mode': 'none',
        'tool_history_mode': 'summary_only',
        'max_history_messages': 4,
        'max_execution_events': 5,
        'max_task_episodes': 2,
        'max_semantic_memories': 2,
        'max_working_memory_chars': 2200,
        'max_project_knowledge_chars': 1800,
        'max_transfer_block_chars': 9000,
    },
    'lmstudio': {
        'profile_name': 'local-64k',
        'max_context_tokens': 65536,
        'safe_prompt_fraction': 0.24,
        'preferred_style': 'summary_first',
        'supports_history_replay': False,
        'supports_tool_result_replay': 'weak',
        'message_mode': 'none',
        'tool_history_mode': 'summary_only',
        'max_history_messages': 4,
        'max_execution_events': 5,
        'max_task_episodes': 2,
        'max_semantic_memories': 2,
        'max_working_memory_chars': 2200,
        'max_project_knowledge_chars': 1800,
        'max_transfer_block_chars': 9000,
    },
    'ollama': {
        'profile_name': 'local-64k',
        'max_context_tokens': 65536,
        'safe_prompt_fraction': 0.24,
        'preferred_style': 'summary_first',
        'supports_history_replay': False,
        'supports_tool_result_replay': 'weak',
        'message_mode': 'none',
        'tool_history_mode': 'summary_only',
        'max_history_messages': 4,
        'max_execution_events': 5,
        'max_task_episodes': 2,
        'max_semantic_memories': 2,
        'max_working_memory_chars': 2200,
        'max_project_knowledge_chars': 1800,
        'max_transfer_block_chars': 9000,
    },
}


MODEL_CONTEXT_OVERRIDES: dict[str, int] = {
    'gpt-4.1': 1000000,
    'gpt-4o': 128000,
    'gpt-4o-mini': 128000,
    'o3': 200000,
    'o4-mini': 200000,
    'o3-mini': 200000,
    'codex-mini-latest': 200000,
    'gpt-5': 200000,
    'gpt-5.4': 200000,
    'claude-sonnet-4-20250514': 200000,
    'claude-opus-4-20250514': 200000,
    'qwen3-30b-a3b': 65536,
}


def _xfer_dir(state_dir: Path) -> Path:
    d = Path(state_dir) / 'transfers'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _transfer_events_path(state_dir: Path) -> Path:
    return _xfer_dir(state_dir) / 'events.jsonl'


def _bundle_id() -> str:
    return f"xfer-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _run_git(project_root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ['git', *args], cwd=str(project_root), capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            return (proc.stdout or '').strip()
    except Exception:
        pass
    return ''


def _git_snapshot(project_root: Path) -> dict[str, Any]:
    return {
        'branch': _run_git(project_root, 'branch', '--show-current'),
        'head': _run_git(project_root, 'rev-parse', 'HEAD'),
        'status': _run_git(project_root, 'status', '--short'),
        'diff_stat': _run_git(project_root, 'diff', '--stat'),
    }


def _shorten(text: Any, limit: int) -> str:
    s = str(text or '').strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 16)] + '\n...[truncated]'


def _extract_task_state(messages: list[Message]) -> dict[str, Any]:
    user_messages = [m for m in messages if m.role == 'user' and isinstance(m.content, str) and str(m.content).strip()]
    assistant_messages = [m for m in messages if m.role == 'assistant' and isinstance(m.content, str) and str(m.content).strip()]
    objective = user_messages[0].content.strip() if user_messages else ''
    latest_user = user_messages[-1].content.strip() if user_messages else ''
    latest_assistant = assistant_messages[-1].content.strip() if assistant_messages else ''

    decisions: list[str] = []
    if assistant_messages:
        for msg in assistant_messages[-4:]:
            text = str(msg.content).strip()
            if not text:
                continue
            first = text.splitlines()[0].strip()
            if first and first not in decisions:
                decisions.append(first[:220])

    return {
        'objective': objective[:1200],
        'latest_user': latest_user[:1200],
        'latest_assistant': latest_assistant[:1200],
        'decisions': decisions[:6],
    }


def _serialize_messages(messages: list[Message], truncate: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        d = message_to_dict(msg)
        if truncate and isinstance(d.get('content'), str) and len(d['content']) > 4000:
            d['content'] = d['content'][:4000] + '\n...[truncated]...'
        if truncate and 'thinking' in d and len(str(d['thinking'])) > 1200:
            d['thinking'] = str(d['thinking'])[:1200] + '...[truncated]'
        out.append(d)
    return out


def _recent_history(messages: list[Message], limit: int = 12) -> list[dict[str, Any]]:
    return _serialize_messages(messages[-limit:], truncate=True)


def _load_working_memory(state_dir: Path, agent_id: str | None) -> dict[str, Any]:
    if not agent_id:
        return {}
    path = state_dir / 'agents' / agent_id / 'working_memory.json'
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_project_knowledge(state_dir: Path, project_root: Path) -> str:
    try:
        from project_registry import ensure_project
        proj = ensure_project(state_dir, project_root)
        pid = str(proj.get('id') or '').strip()
        if pid:
            canonical = state_dir / 'projects' / pid / 'KNOWLEDGE.md'
            if canonical.exists():
                try:
                    text = canonical.read_text(encoding='utf-8').strip()
                    if text:
                        return text[:4000]
                except Exception:
                    pass
    except Exception:
        pass

    candidates = [
        state_dir / 'projects',
        state_dir / 'project',
    ]
    for root in candidates:
        if root.exists():
            for md in root.rglob('KNOWLEDGE.md'):
                try:
                    text = md.read_text(encoding='utf-8').strip()
                    if text:
                        return text[:4000]
                except Exception:
                    pass
    fallback = project_root / '.charon' / 'PROJECT_KNOWLEDGE.md'
    if fallback.exists():
        try:
            return fallback.read_text(encoding='utf-8').strip()[:4000]
        except Exception:
            pass
    return ''


def session_has_transferable_context(messages: list[Message]) -> bool:
    if len(messages) >= 4:
        return True
    tool_calls = 0
    for msg in messages:
        tool_calls += len(msg.tool_calls or [])
        if msg.role == 'tool_result':
            tool_calls += 1
    return tool_calls > 0


def _save_full_transcript(state_dir: Path, bundle_id: str, messages: list[Message]) -> str:
    path = _xfer_dir(state_dir) / f'{bundle_id}-full-messages.json'
    path.write_text(json.dumps(_serialize_messages(messages, truncate=False), indent=2, ensure_ascii=False), encoding='utf-8')
    return str(path)


def record_transfer_event(state_dir: Path | str, event: dict[str, Any]) -> None:
    path = _transfer_events_path(Path(state_dir))
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(event, ensure_ascii=False) + '\n')


def list_transfer_events(state_dir: Path | str, limit: int = 20) -> list[dict[str, Any]]:
    path = _transfer_events_path(Path(state_dir))
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        except Exception:
            continue
    return rows[-limit:]


def create_transfer_bundle(
    *,
    state_dir: Path | str,
    session_id: str,
    agent_id: str,
    project_root: Path | str,
    source_provider: str,
    target_provider: str,
    messages: list[Message],
) -> dict[str, Any]:
    state_dir = Path(state_dir)
    project_root = Path(project_root).resolve()

    from execution_memory import (
        get_recent_tool_events, search_execution_memories,
        get_recent_task_episodes, extract_touched_files,
        get_last_validation_event,
    )

    bundle_id = _bundle_id()
    task = _extract_task_state(messages)
    recent_events = get_recent_tool_events(state_dir, session_id=session_id, limit=16)
    query = task.get('latest_user') or task.get('objective') or 'current task'
    relevant_exec = search_execution_memories(
        state_dir,
        query=query,
        project_root=str(project_root),
        limit=8,
    )
    recent_episodes = get_recent_task_episodes(state_dir, session_id=session_id, limit=4)
    last_validation = get_last_validation_event(state_dir, session_id=session_id, limit=40)
    working_memory = _load_working_memory(state_dir, agent_id)
    project_knowledge = _load_project_knowledge(state_dir, project_root)
    git = _git_snapshot(project_root)

    files_touched: list[str] = extract_touched_files(recent_events)
    if git.get('status'):
        for line in str(git['status']).splitlines():
            line = line.strip()
            if len(line) > 3:
                candidate = line[3:].strip()
                if candidate and candidate not in files_touched:
                    files_touched.append(candidate)

    full_transcript_path = _save_full_transcript(state_dir, bundle_id, messages)

    bundle = {
        'id': bundle_id,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'source': {
            'provider': source_provider,
            'session_id': session_id,
            'agent_id': agent_id,
        },
        'target': {
            'provider': target_provider,
        },
        'task': {
            'objective': task.get('objective', ''),
            'status': 'in_progress',
            'latest_user': task.get('latest_user', ''),
            'latest_assistant': task.get('latest_assistant', ''),
            'next_step': task.get('latest_assistant', '')[:500],
        },
        'state': {
            'decisions': task.get('decisions', []),
            'working_memory_summary': str(working_memory.get('last_task_summary') or '')[:2000],
            'working_memory_notes': (working_memory.get('notes') or [])[:20],
        },
        'workspace': {
            'project_root': str(project_root),
            'git': git,
            'files_touched': files_touched[:40],
            'last_validation': last_validation or {},
        },
        'history': {
            'normalized_messages': _recent_history(messages, limit=12),
            'full_transcript_path': full_transcript_path,
            'full_message_count': len(messages),
        },
        'execution': {
            'recent_tool_events': recent_events,
            'relevant_execution_memories': relevant_exec,
            'recent_task_episodes': recent_episodes,
        },
        'memory': {
            'project_knowledge': project_knowledge,
        },
        'fidelity': {
            'normalized_history': True,
            'execution_events': True,
            'semantic_execution_recall': bool(relevant_exec),
            'git_snapshot': True,
        },
    }

    json_path = _xfer_dir(state_dir) / f'{bundle_id}.json'
    md_path = _xfer_dir(state_dir) / f'{bundle_id}.md'
    json_path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding='utf-8')
    md_path.write_text(render_transfer_markdown(bundle), encoding='utf-8')
    record_transfer_event(state_dir, {
        'ts': bundle['created_at'],
        'type': 'bundle_created',
        'bundle_id': bundle_id,
        'source_provider': source_provider,
        'target_provider': target_provider,
        'session_id': session_id,
        'objective': task.get('objective', '')[:180],
        'files_touched': len(files_touched),
    })
    return bundle


def render_transfer_markdown(bundle: dict[str, Any]) -> str:
    task = bundle.get('task', {})
    state = bundle.get('state', {})
    workspace = bundle.get('workspace', {})
    git = workspace.get('git', {})
    execution = bundle.get('execution', {})

    lines = [
        f"# Context Transfer {bundle.get('id', '')}",
        '',
        f"- Source: {bundle.get('source', {}).get('provider', '')}",
        f"- Target: {bundle.get('target', {}).get('provider', '')}",
        f"- Created: {bundle.get('created_at', '')}",
        '',
        '## Objective',
        task.get('objective', '') or '(none)',
        '',
        '## Current Status',
        task.get('latest_assistant', '') or '(none)',
        '',
        '## Decisions',
    ]
    for d in state.get('decisions', []) or []:
        lines.append(f'- {d}')
    if not state.get('decisions'):
        lines.append('- (none)')
    lines += [
        '',
        '## Workspace',
        f"- Project: {workspace.get('project_root', '')}",
        f"- Branch: {git.get('branch', '')}",
        f"- Head: {git.get('head', '')}",
    ]
    validation = workspace.get('last_validation', {}) or {}
    if validation.get('command'):
        lines += [
            f"- Last validation: {validation.get('status', 'unknown')} `{validation.get('command', '')}`",
            f"- Validation summary: {validation.get('summary', '')}",
        ]
    lines += [
        '',
        '## Files Touched',
    ]
    for path in workspace.get('files_touched', []) or []:
        lines.append(f'- {path}')
    if not workspace.get('files_touched'):
        lines.append('- (none)')
    lines += ['', '## Recent Execution Evidence']
    for ev in execution.get('recent_tool_events', [])[:10]:
        lines.append(f"- {ev.get('summary', '')}")
    if not execution.get('recent_tool_events'):
        lines.append('- (none)')
    episodes = execution.get('recent_task_episodes', []) or []
    lines += ['', '## Recent Task Episodes']
    for ep in episodes[:4]:
        lines.append(f"- {ep.get('objective', '')[:120]} → {ep.get('summary', '')[:180]}")
    if not episodes:
        lines.append('- (none)')
    history = bundle.get('history', {})
    lines += ['', '## Full Transcript']
    lines.append(f"- Messages: {history.get('full_message_count', 0)}")
    lines.append(f"- Path: {history.get('full_transcript_path', '')}")
    return '\n'.join(lines).strip() + '\n'


def build_transfer_block(bundle: dict[str, Any]) -> str:
    compiled = compile_transfer_bundle(
        bundle,
        resolve_transfer_profile(bundle.get('target', {}).get('provider', '')),
        estimate_transfer_budget(resolve_transfer_profile(bundle.get('target', {}).get('provider', ''))),
    )
    return render_compiled_transfer_block(compiled)


def _provider_aliases(provider_name: str) -> list[str]:
    raw = str(provider_name or '').strip().lower()
    names = [raw]
    if raw in ('anthropic', 'claude', 'claude-code'):
        names.extend(['claude-code', 'anthropic'])
    elif raw in ('openai', 'codex', 'openai-codex'):
        names.extend(['codex', 'openai'])
    elif raw in ('local', 'lmstudio', 'ollama', 'openlm'):
        names.extend(['openlm', 'local', 'lmstudio', 'ollama'])
    return [n for i, n in enumerate(names) if n and n not in names[:i]]


def resolve_transfer_profile(provider_name: str, model_name: str | None = None) -> dict[str, Any]:
    profile = dict(TRANSFER_PROFILES['default'])
    for candidate in _provider_aliases(provider_name):
        if candidate in TRANSFER_PROFILES:
            profile.update(TRANSFER_PROFILES[candidate])
            break

    model_name = str(model_name or '').strip()
    model_lower = model_name.lower()
    if model_name:
        profile['model_name'] = model_name
    if model_name and model_name in MODEL_CONTEXT_OVERRIDES:
        profile['max_context_tokens'] = MODEL_CONTEXT_OVERRIDES[model_name]
    elif 'claude' in model_lower:
        profile['max_context_tokens'] = max(profile.get('max_context_tokens', 0), 200000)
        profile['profile_name'] = 'claude-large'
    elif 'codex' in model_lower:
        profile['max_context_tokens'] = max(profile.get('max_context_tokens', 0), 200000)
        profile['profile_name'] = 'codex-large'
    elif any(k in model_lower for k in ('qwen', 'llama', 'mistral', 'gemma', 'openlm')):
        profile['max_context_tokens'] = MODEL_CONTEXT_OVERRIDES.get(model_name, profile.get('max_context_tokens', 65536))
        profile['preferred_style'] = 'summary_first'
        profile['supports_history_replay'] = False
        profile['message_mode'] = 'none'
        profile['tool_history_mode'] = 'summary_only'
    return profile


def estimate_text_tokens(text: str) -> int:
    text = str(text or '')
    return max(1, len(text) // 4) if text else 0


def estimate_transfer_budget(profile: dict[str, Any], engine: Any | None = None) -> int:
    max_context = int(profile.get('max_context_tokens', 65536) or 65536)
    if engine is not None:
        try:
            model_ctx = int(getattr(getattr(engine, 'model', None), 'context_window', 0) or 0)
            if model_ctx:
                max_context = model_ctx
        except Exception:
            pass
    budget = int(max_context * float(profile.get('safe_prompt_fraction', 0.28) or 0.28))

    if engine is not None:
        try:
            budget -= estimate_text_tokens(getattr(engine, 'system_prompt', '') or '')
        except Exception:
            pass

    tool_schema_reserve = 3500
    output_reserve = max(4096, max_context // 10)
    safety_margin = max(2048, max_context // 20)
    budget -= tool_schema_reserve + output_reserve + safety_margin
    return max(2500, budget)


def _history_tier_for_budget(profile: dict[str, Any], budget_tokens: int) -> str:
    preferred = str(profile.get('preferred_style', 'hybrid'))
    if budget_tokens > 40000:
        tier = 'rich'
    elif budget_tokens > 18000:
        tier = 'standard'
    elif budget_tokens > 8000:
        tier = 'compressed'
    else:
        tier = 'minimal'
    if preferred == 'summary_first' and tier == 'rich':
        tier = 'standard'
    return tier


def _trim_list(items: list[Any], limit: int) -> list[Any]:
    if limit <= 0:
        return []
    return list(items[:limit])


def _working_memory_excerpt(bundle: dict[str, Any], max_chars: int) -> str:
    state = bundle.get('state', {})
    pieces: list[str] = []
    summary = str(state.get('working_memory_summary') or '').strip()
    if summary:
        pieces.append(summary)
    notes = state.get('working_memory_notes') or []
    if notes:
        note_lines = [f"- {str(n)[:180]}" for n in notes[:8] if str(n).strip()]
        if note_lines:
            pieces.append('Notes:\n' + '\n'.join(note_lines))
    return _shorten('\n\n'.join(pieces), max_chars)


def _semantic_memory_lines(bundle: dict[str, Any], limit: int, per_item_chars: int = 220) -> list[str]:
    lines: list[str] = []
    for mem in (bundle.get('execution', {}).get('relevant_execution_memories', []) or [])[:limit]:
        content = str(mem.get('content') or '').strip()
        if content:
            lines.append(_shorten(content.splitlines()[0], per_item_chars))
    return lines


def _flatten_execution_lines(bundle: dict[str, Any], limit: int, summary_only: bool = False) -> list[str]:
    execution = bundle.get('execution', {})
    lines: list[str] = []
    for ev in (execution.get('recent_tool_events', []) or [])[:limit]:
        summary = str(ev.get('summary') or '').strip()
        if summary:
            lines.append(_shorten(summary, 180 if summary_only else 260))
    if not summary_only:
        for ep in (execution.get('recent_task_episodes', []) or [])[: max(1, limit // 3)]:
            summary = str(ep.get('summary') or ep.get('objective') or '').strip()
            if summary:
                lines.append('episode: ' + _shorten(summary, 220))
    deduped: list[str] = []
    for line in lines:
        if line and line not in deduped:
            deduped.append(line)
    return deduped[:limit]


def filter_portable_messages(messages: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    mode = str(profile.get('message_mode', 'assistant_user_only'))
    out: list[dict[str, Any]] = []
    for item in messages:
        role = str(item.get('role') or '')
        if mode == 'none':
            continue
        if mode == 'user_only' and role != 'user':
            continue
        if mode == 'assistant_user_only' and role not in ('assistant', 'user'):
            continue
        if mode == 'full' and role not in ('assistant', 'user', 'tool_result'):
            continue
        cleaned = dict(item)
        cleaned.pop('thinking', None)
        if role != 'tool_result':
            cleaned.pop('tool_name', None)
            cleaned.pop('tool_call_id', None)
            cleaned.pop('is_error', None)
        if role == 'assistant' and str(profile.get('supports_tool_result_replay', 'weak')) == 'weak':
            cleaned['tool_calls'] = []
        out.append(cleaned)
    max_messages = int(profile.get('max_history_messages', 8) or 8)
    if len(out) > max_messages:
        out = out[-max_messages:]
    return out


def compile_transfer_bundle(bundle: dict[str, Any], profile: dict[str, Any], budget_tokens: int) -> dict[str, Any]:
    task = bundle.get('task', {})
    state = bundle.get('state', {})
    workspace = bundle.get('workspace', {})
    git = workspace.get('git', {})
    history = bundle.get('history', {})

    tier = _history_tier_for_budget(profile, budget_tokens)
    if tier == 'rich':
        history_limit = min(int(profile.get('max_history_messages', 12)), 16)
        exec_limit = int(profile.get('max_execution_events', 10))
        episode_limit = int(profile.get('max_task_episodes', 4))
        semantic_limit = int(profile.get('max_semantic_memories', 4))
        wm_chars = int(profile.get('max_working_memory_chars', 8000))
        pk_chars = int(profile.get('max_project_knowledge_chars', 7000))
    elif tier == 'standard':
        history_limit = min(int(profile.get('max_history_messages', 10)), 10)
        exec_limit = min(int(profile.get('max_execution_events', 8)), 8)
        episode_limit = min(int(profile.get('max_task_episodes', 3)), 3)
        semantic_limit = min(int(profile.get('max_semantic_memories', 3)), 3)
        wm_chars = min(int(profile.get('max_working_memory_chars', 5000)), 5000)
        pk_chars = min(int(profile.get('max_project_knowledge_chars', 4000)), 4000)
    elif tier == 'compressed':
        history_limit = min(int(profile.get('max_history_messages', 4)), 4)
        exec_limit = min(int(profile.get('max_execution_events', 5)), 5)
        episode_limit = min(int(profile.get('max_task_episodes', 2)), 2)
        semantic_limit = min(int(profile.get('max_semantic_memories', 2)), 2)
        wm_chars = min(int(profile.get('max_working_memory_chars', 2500)), 2500)
        pk_chars = min(int(profile.get('max_project_knowledge_chars', 2000)), 2000)
    else:
        history_limit = 1 if profile.get('message_mode') == 'user_only' else 0
        exec_limit = 3
        episode_limit = 1
        semantic_limit = 1
        wm_chars = 1200
        pk_chars = 0

    local_profile = dict(profile)
    local_profile['max_history_messages'] = history_limit
    restore_messages = filter_portable_messages(history.get('normalized_messages', []) or [], local_profile)

    if tier in ('compressed', 'minimal') and profile.get('supports_history_replay') is False:
        restore_messages = []

    files_touched = _trim_list(workspace.get('files_touched', []) or [], 12 if tier in ('rich', 'standard') else 8)
    execution_lines = _flatten_execution_lines(
        bundle,
        exec_limit,
        summary_only=str(profile.get('tool_history_mode', 'flattened')) == 'summary_only',
    )
    episodes = []
    for ep in (bundle.get('execution', {}).get('recent_task_episodes', []) or [])[:episode_limit]:
        objective = _shorten(ep.get('objective', ''), 120)
        summary = _shorten(ep.get('summary', ''), 180)
        text = objective
        if summary:
            text = f"{objective} → {summary}" if objective else summary
        if text:
            episodes.append(text)

    semantic_memories = _semantic_memory_lines(bundle, semantic_limit)
    project_knowledge = _shorten(bundle.get('memory', {}).get('project_knowledge', ''), pk_chars)
    working_memory = _working_memory_excerpt(bundle, wm_chars)

    last_validation = workspace.get('last_validation', {}) or {}
    validation = ''
    if last_validation.get('command'):
        validation = f"{last_validation.get('status', 'unknown')}: `{last_validation.get('command', '')}`"
        if last_validation.get('summary'):
            validation += f" → {last_validation.get('summary', '')}"
        validation = _shorten(validation, 900 if tier in ('rich', 'standard') else 400)
    elif git.get('diff_stat'):
        validation = _shorten(git.get('diff_stat', ''), 900 if tier in ('rich', 'standard') else 400)
    elif execution_lines:
        validation = execution_lines[0]

    sections = {
        'objective': _shorten(task.get('objective', ''), 1200),
        'current_status': _shorten(task.get('latest_assistant') or task.get('latest_user') or '', 1200),
        'next_step': _shorten(task.get('next_step') or task.get('latest_assistant') or '', 600 if tier in ('rich', 'standard') else 320),
        'decisions': _trim_list(state.get('decisions', []) or [], 6 if tier in ('rich', 'standard') else 3),
        'files_touched': files_touched,
        'validation': validation,
        'execution_bullets': execution_lines,
        'episodes': episodes,
        'working_memory_excerpt': working_memory,
        'project_knowledge_excerpt': project_knowledge,
        'semantic_memories': semantic_memories,
        'history_excerpt': restore_messages,
        'transcript_ref': {
            'full_message_count': int(history.get('full_message_count', 0) or 0),
            'full_transcript_path': str(history.get('full_transcript_path') or ''),
        },
        'workspace': {
            'project_root': str(workspace.get('project_root') or ''),
            'branch': str(git.get('branch') or ''),
            'head': str(git.get('head') or '')[:16],
            'last_validation': last_validation,
        },
    }

    compiled = {
        'bundle_id': bundle.get('id', ''),
        'source_provider': bundle.get('source', {}).get('provider', ''),
        'target_provider': bundle.get('target', {}).get('provider', ''),
        'profile_name': profile.get('profile_name', 'default-standard'),
        'model_name': profile.get('model_name', ''),
        'max_context_tokens': int(profile.get('max_context_tokens', 65536) or 65536),
        'budget_tokens': int(budget_tokens),
        'tier': tier,
        'strategy': {
            'preferred_style': profile.get('preferred_style', 'hybrid'),
            'message_mode': profile.get('message_mode', 'assistant_user_only'),
            'tool_history_mode': profile.get('tool_history_mode', 'flattened'),
            'supports_history_replay': bool(profile.get('supports_history_replay', True)),
        },
        'sections': sections,
        'restore_messages': restore_messages,
        'omitted': {
            'older_messages': max(0, len(history.get('normalized_messages', []) or []) - len(restore_messages)),
            'semantic_memories': max(0, len(bundle.get('execution', {}).get('relevant_execution_memories', []) or []) - len(semantic_memories)),
            'episodes': max(0, len(bundle.get('execution', {}).get('recent_task_episodes', []) or []) - len(episodes)),
        },
    }

    block = render_compiled_transfer_block(compiled)
    applied_tokens = estimate_text_tokens(block)
    max_block_chars = int(profile.get('max_transfer_block_chars', 14000) or 14000)

    if len(block) > max_block_chars or applied_tokens > budget_tokens:
        if compiled['sections']['project_knowledge_excerpt']:
            compiled['sections']['project_knowledge_excerpt'] = ''
        if applied_tokens > budget_tokens and compiled['sections']['semantic_memories']:
            compiled['sections']['semantic_memories'] = compiled['sections']['semantic_memories'][:1]
        if applied_tokens > budget_tokens and compiled['sections']['episodes']:
            compiled['sections']['episodes'] = compiled['sections']['episodes'][:1]
        if applied_tokens > budget_tokens and tier in ('compressed', 'minimal'):
            compiled['restore_messages'] = []
            compiled['sections']['history_excerpt'] = []
        block = render_compiled_transfer_block(compiled)
        applied_tokens = estimate_text_tokens(block)

    compiled['applied_tokens_estimate'] = applied_tokens
    compiled['replayed_messages'] = len(compiled.get('restore_messages', []) or [])
    return compiled


def render_compiled_transfer_block(compiled: dict[str, Any]) -> str:
    sections = compiled.get('sections', {})
    workspace = sections.get('workspace', {})
    transcript_ref = sections.get('transcript_ref', {})
    lines = [
        '[CONTEXT TRANSFER]',
        f"Source provider: {compiled.get('source_provider', '')}",
        f"Target provider: {compiled.get('target_provider', '')}",
        f"Transfer profile: {compiled.get('profile_name', '')}",
        f"Transfer tier: {compiled.get('tier', '')}",
        '',
        'Objective:',
        sections.get('objective', '') or '(none)',
        '',
        'Current status:',
        sections.get('current_status', '') or '(none)',
    ]

    next_step = sections.get('next_step', '')
    if next_step:
        lines += ['', 'Next step:', next_step]

    decisions = sections.get('decisions', []) or []
    if decisions:
        lines += ['', 'Decisions already made:']
        for d in decisions:
            lines.append(f'- {d}')

    lines += [
        '',
        'Workspace state:',
        f"- project_root: {workspace.get('project_root', '')}",
        f"- branch: {workspace.get('branch', '')}",
        f"- head: {workspace.get('head', '')}",
    ]
    last_validation = workspace.get('last_validation', {}) or {}
    if last_validation.get('command'):
        lines.append(f"- last_validation: {last_validation.get('status', 'unknown')} `{last_validation.get('command', '')}`")

    files_touched = sections.get('files_touched', []) or []
    lines += ['', 'Relevant files:']
    for path in files_touched:
        lines.append(f'- {path}')
    if not files_touched:
        lines.append('- (none)')

    validation = sections.get('validation', '')
    if validation:
        lines += ['', 'Latest validation / workspace evidence:', validation]

    execution_bullets = sections.get('execution_bullets', []) or []
    if execution_bullets:
        lines += ['', 'Recent important actions:']
        for line in execution_bullets:
            lines.append(f'- {line}')

    episodes = sections.get('episodes', []) or []
    if episodes:
        lines += ['', 'Recent task episodes:']
        for line in episodes:
            lines.append(f'- {line}')

    semantic_memories = sections.get('semantic_memories', []) or []
    if semantic_memories:
        lines += ['', 'Related recalled memories:']
        for line in semantic_memories:
            lines.append(f'- {line}')

    working_memory = sections.get('working_memory_excerpt', '')
    if working_memory:
        lines += ['', 'Working memory snapshot:', working_memory]

    project_knowledge = sections.get('project_knowledge_excerpt', '')
    if project_knowledge:
        lines += ['', 'Project knowledge snapshot:', project_knowledge]

    history_excerpt = sections.get('history_excerpt', []) or []
    if history_excerpt:
        lines += ['', 'Recent transcript excerpt:']
        for item in history_excerpt[-4:]:
            role = str(item.get('role') or 'message')
            content = _shorten(item.get('content', ''), 260)
            if content:
                lines.append(f'- {role}: {content}')

    lines += [
        '',
        'Full transcript artifact:',
        f"- messages: {transcript_ref.get('full_message_count', 0)}",
        f"- path: {transcript_ref.get('full_transcript_path', '')}",
        '',
        'Instructions:',
        'Continue this same logical session.',
        'Prefer current repository state over inferred transcript details if they conflict.',
        'Use the preserved files, validations, and recent actions to continue the task without asking to restate prior work.',
        'If any transferred detail seems inconsistent, say so briefly and proceed from the repo state.',
        '[/CONTEXT TRANSFER]',
    ]
    return '\n'.join(lines)


def apply_transfer_to_engine(engine: Any, bundle: dict[str, Any]) -> None:
    """Inject transfer context into a newly created engine.

    Strategy:
    - compile the rich bundle for the destination provider/model/context budget
    - append compiled transfer block to system prompt
    - restore only provider-safe recent messages
    - preserve both raw and compiled transfer metadata on the engine
    """
    from conversation_store import dict_to_message

    provider_name = getattr(engine, 'provider_name', '') or bundle.get('target', {}).get('provider', '')
    model_name = getattr(getattr(engine, 'model', None), 'model_id', '') or None
    profile = resolve_transfer_profile(provider_name, model_name)
    budget_tokens = estimate_transfer_budget(profile, engine)
    compiled = compile_transfer_bundle(bundle, profile, budget_tokens)

    transfer_block = render_compiled_transfer_block(compiled)
    if transfer_block not in engine.system_prompt:
        engine.system_prompt = f"{engine.system_prompt}\n\n{transfer_block}"

    restored: list[Message] = []
    for item in compiled.get('restore_messages', []) or []:
        try:
            restored.append(dict_to_message(item))
        except Exception:
            continue
    if restored:
        engine.messages = restored

    engine.transfer_bundle = bundle
    engine.transfer_compiled = compiled


def record_pending_transfer(state_dir: Path | str, bundle: dict[str, Any]) -> None:
    path = Path(state_dir) / 'pending_transfer.json'
    path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding='utf-8')


def load_pending_transfer(state_dir: Path | str) -> dict[str, Any] | None:
    path = Path(state_dir) / 'pending_transfer.json'
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def clear_pending_transfer(state_dir: Path | str) -> None:
    path = Path(state_dir) / 'pending_transfer.json'
    try:
        path.unlink()
    except FileNotFoundError:
        pass
