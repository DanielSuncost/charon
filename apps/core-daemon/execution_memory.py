"""Execution memory — raw tool event log + semantic indexing.

This module captures meaningful tool activity so provider handoffs can be
rebuilt from actual actions, not only chat summaries.

Storage:
- Raw append-only JSONL: .charon_state/execution/tool_events.jsonl
- Task episodes JSONL:    .charon_state/execution/task_episodes.jsonl
- Per-event blobs:        .charon_state/execution/blobs/<event-id>.txt
- Semantic index:         MemoryEngine category=execution/tool event summaries
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


def _execution_dir(state_dir: Path) -> Path:
    d = Path(state_dir) / 'execution'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _events_path(state_dir: Path) -> Path:
    return _execution_dir(state_dir) / 'tool_events.jsonl'


def _blob_dir(state_dir: Path) -> Path:
    d = _execution_dir(state_dir) / 'blobs'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _episodes_path(state_dir: Path) -> Path:
    return _execution_dir(state_dir) / 'task_episodes.jsonl'


def _now() -> float:
    return time.time()


def _event_id() -> str:
    return f"evt-{uuid.uuid4().hex[:12]}"


def _safe_json(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True)
    except Exception:
        return repr(data)


IMPORTANT_TOOLS = {
    'Read', 'Write', 'Edit', 'Bash', 'Git', 'Search', 'Recall', 'Web', 'Browser',
}


def _command_kind(command: str) -> str:
    c = (command or '').strip().lower()
    if not c:
        return 'bash'
    if any(k in c for k in ('pytest', 'cargo test', 'npm test', 'pnpm test', 'uv run pytest', 'go test')):
        return 'test'
    if any(k in c for k in ('ruff', 'flake8', 'eslint', 'mypy', 'pyright', 'cargo clippy', 'cargo fmt', 'prettier')):
        return 'lint'
    if any(k in c for k in ('build', 'compile', 'cargo build', 'npm run build', 'pnpm build')):
        return 'build'
    if any(k in c for k in ('git ', 'git\n')):
        return 'git'
    return 'bash'


def classify_importance(tool_name: str, params: dict, result_preview: str, is_error: bool) -> int:
    tool_name = str(tool_name or '')
    if tool_name in ('Write', 'Edit', 'Git'):
        return 90
    if tool_name == 'Bash':
        command = str((params or {}).get('command') or '')
        kind = _command_kind(command)
        if kind in ('test', 'lint', 'build', 'git'):
            return 85
        return 60
    if tool_name in ('Search', 'Recall', 'Web', 'Browser'):
        return 70
    if tool_name == 'Read':
        path = str((params or {}).get('path') or '')
        if path.endswith(('.py', '.rs', '.ts', '.tsx', '.js', '.md', '.toml', '.json', '.yaml', '.yml')):
            return 45
        return 25
    score = 40 if tool_name in IMPORTANT_TOOLS else 15
    if is_error:
        score += 10
    if len(result_preview or '') > 200:
        score += 5
    return min(score, 100)


def summarize_tool_event(tool_name: str, params: dict, result_preview: str, is_error: bool) -> tuple[str, list[str]]:
    tool_name = str(tool_name or '')
    params = params or {}
    result_preview = (result_preview or '').strip().replace('\r', '')
    first_line = result_preview.splitlines()[0][:220] if result_preview else ''
    tags: list[str] = [tool_name.lower()]

    if tool_name == 'Read':
        path = str(params.get('path') or '')
        summary = f"Read {path}."
        if path:
            tags.extend(['read', path])
        return summary, tags

    if tool_name == 'Write':
        path = str(params.get('path') or '')
        summary = f"Wrote file {path}."
        if path:
            tags.extend(['write', path])
        return summary, tags

    if tool_name == 'Edit':
        path = str(params.get('path') or '')
        summary = f"Edited file {path}."
        if path:
            tags.extend(['edit', path])
        return summary, tags

    if tool_name == 'Bash':
        command = str(params.get('command') or '')
        kind = _command_kind(command)
        tags.extend(['bash', kind])
        prefix = 'Failed command' if is_error else 'Ran command'
        summary = f"{prefix}: `{command[:180]}`"
        if first_line:
            summary += f" → {first_line}"
        return summary, tags

    if tool_name == 'Git':
        action = str(params.get('action') or '')
        summary = f"Ran git action `{action}`"
        if first_line:
            summary += f" → {first_line}"
        tags.extend(['git', action])
        return summary, tags

    if tool_name in ('Search', 'Recall'):
        query = str(params.get('query') or '')
        summary = f"Used {tool_name} for query `{query[:180]}`"
        if first_line:
            summary += f" → {first_line}"
        tags.extend(['query', query])
        return summary, tags

    if tool_name in ('Web', 'Browser'):
        url = str(params.get('url') or params.get('query') or params.get('action') or '')
        summary = f"Used {tool_name} on `{url[:180]}`"
        if first_line:
            summary += f" → {first_line}"
        tags.append('web')
        return summary, tags

    summary = f"Used tool {tool_name}"
    if first_line:
        summary += f" → {first_line}"
    return summary, tags


def record_tool_event(
    state_dir: Path | str,
    *,
    session_id: str,
    agent_id: str,
    provider: str,
    tool_name: str,
    params: dict,
    result_content: str,
    is_error: bool,
    project_root: str,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    """Record a tool event and best-effort index it semantically."""
    state_dir = Path(state_dir)
    event_id = _event_id()
    full_result = result_content or ''
    preview = full_result[:4000]
    importance = classify_importance(tool_name, params, preview, is_error)
    summary, tags = summarize_tool_event(tool_name, params, preview, is_error)

    blob_ref = ''
    if len(full_result) > 4000:
        blob_path = _blob_dir(state_dir) / f'{event_id}.txt'
        blob_path.write_text(full_result)
        blob_ref = str(blob_path)

    event = {
        'id': event_id,
        'ts': _now(),
        'session_id': session_id,
        'agent_id': agent_id,
        'provider': provider,
        'project_root': project_root,
        'tool_name': tool_name,
        'params': params or {},
        'result_preview': preview,
        'blob_ref': blob_ref,
        'is_error': bool(is_error),
        'duration_ms': duration_ms or 0,
        'importance': importance,
        'summary': summary,
        'tags': tags,
    }

    path = _events_path(state_dir)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(event, ensure_ascii=False) + '\n')

    # Best-effort semantic indexing.
    if importance >= 45:
        try:
            from memory_engine import MemoryEngine
            engine = MemoryEngine(state_dir)
            project_tag = f"project:{Path(project_root).resolve()}"
            content = f"{summary}\nTool={tool_name}\nParams={_safe_json(params)[:800]}"
            engine.add(
                content,
                category='execution',
                tier='agent',
                container_tag=project_tag,
                source_agent=agent_id,
                source_conv=session_id,
                check_updates=False,
            )
        except Exception:
            pass

    return event


def _load_events(state_dir: Path | str) -> list[dict[str, Any]]:
    path = _events_path(Path(state_dir))
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                events.append(obj)
        except Exception:
            continue
    return events


def _load_episodes(state_dir: Path | str) -> list[dict[str, Any]]:
    path = _episodes_path(Path(state_dir))
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except Exception:
            continue
    return rows


def get_recent_tool_events(
    state_dir: Path | str,
    *,
    session_id: str | None = None,
    limit: int = 20,
    min_importance: int = 35,
) -> list[dict[str, Any]]:
    events = _load_events(state_dir)
    if session_id:
        events = [e for e in events if e.get('session_id') == session_id]
    events = [e for e in events if int(e.get('importance', 0)) >= min_importance]
    return events[-limit:]


def extract_touched_files(events: list[dict[str, Any]]) -> list[str]:
    files: list[str] = []
    for ev in events:
        params = ev.get('params', {}) or {}
        for key in ('path', 'file', 'target'):
            value = str(params.get(key) or '').strip()
            if value and value not in files:
                files.append(value)
        command = str(params.get('command') or '').strip()
        if command and command not in files and command.startswith(('pytest ', 'cargo ', 'npm ', 'pnpm ', 'uv ', 'python ')):
            files.append(command[:200])
    return files


def _is_validation_command(command: str) -> bool:
    c = str(command or '').strip().lower()
    if not c:
        return False
    markers = (
        'pytest', 'py.test', 'python -m py_compile', 'python -m unittest',
        'cargo test', 'cargo check', 'cargo clippy', 'cargo build',
        'npm test', 'npm run test', 'npm run build',
        'pnpm test', 'pnpm run test', 'pnpm build', 'pnpm run build',
        'yarn test', 'yarn build', 'ruff check', 'mypy', 'pyright',
        'eslint', 'tsc', 'go test', 'go build', 'uv run pytest',
    )
    return any(marker in c for marker in markers)


def summarize_validation_event(event: dict[str, Any]) -> dict[str, Any]:
    params = event.get('params', {}) or {}
    command = str(params.get('command') or params.get('args') or '').strip()
    preview = str(event.get('result_preview') or '').strip()
    is_error = bool(event.get('is_error'))
    status = 'failed' if is_error else 'passed'
    if not command:
        status = 'unknown'
    summary = preview.splitlines()[0][:240] if preview else ''
    if not summary:
        summary = str(event.get('summary') or '')[:240]
    return {
        'command': command[:240],
        'tool': str(event.get('tool_name') or ''),
        'status': status,
        'summary': summary,
        'event_id': str(event.get('id') or ''),
        'ts': event.get('ts'),
        'kind': _command_kind(command),
    }


def get_last_validation_event(
    state_dir: Path | str,
    *,
    session_id: str | None = None,
    limit: int = 40,
) -> dict[str, Any] | None:
    events = _load_events(state_dir)
    if session_id:
        events = [e for e in events if e.get('session_id') == session_id]
    for event in reversed(events[-limit:]):
        tool_name = str(event.get('tool_name') or '')
        params = event.get('params', {}) or {}
        command = str(params.get('command') or params.get('args') or '').strip()
        if tool_name == 'Bash' and _is_validation_command(command):
            return summarize_validation_event(event)
    return None


def create_task_episode(
    state_dir: Path | str,
    *,
    session_id: str,
    agent_id: str,
    project_root: str,
    provider: str,
    objective: str,
    summary: str,
    tool_calls: list[dict[str, Any]],
    response_text: str,
    total_turns: int,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, Any]:
    state_dir = Path(state_dir)
    episode_id = f"ep-{uuid.uuid4().hex[:12]}"
    events = get_recent_tool_events(state_dir, session_id=session_id, limit=24, min_importance=45)
    files_touched = extract_touched_files(events)
    record = {
        'id': episode_id,
        'ts': _now(),
        'session_id': session_id,
        'agent_id': agent_id,
        'project_root': str(Path(project_root).resolve()),
        'provider': provider,
        'objective': (objective or '')[:1200],
        'summary': (summary or '')[:3000],
        'response_preview': (response_text or '')[:1200],
        'tool_calls': len(tool_calls or []),
        'turns': total_turns,
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'files_touched': files_touched[:40],
        # tool-call name sequence — raw material for procedural-memory distillation
        'tool_sequence': [
            (tc.get('tool') or tc.get('name') or tc.get('tool_name') or '')
            for tc in (tool_calls or [])
        ][:40],
    }
    path = _episodes_path(state_dir)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')

    try:
        from memory_engine import MemoryEngine
        engine = MemoryEngine(state_dir)
        project_tag = f"project:{Path(project_root).resolve()}"
        content = (
            f"Task episode: {record['objective']}\n"
            f"Summary: {record['summary']}\n"
            f"Files: {', '.join(record['files_touched'][:12])}"
        )
        _ts = record['ts']
        if isinstance(_ts, (int, float)):
            from datetime import datetime, timezone
            event_date = datetime.fromtimestamp(_ts, timezone.utc).date().isoformat()
        else:
            event_date = (str(_ts) if _ts else '')[:10] or None
        mem = engine.add(
            content,
            category='task_episode',
            tier='agent',
            container_tag=project_tag,
            source_agent=agent_id,
            source_conv=session_id,
            event_date=event_date,
            check_updates=False,
        )
        # Promote the task episode to a first-class, time-queryable Episode,
        # reusing the already-indexed memory as its retrievable handle.
        try:
            import episodic
            _ep = episodic.get_or_create_episode_for_session(
                engine, source_conv=session_id, source_agent=agent_id,
                summary=content, member_ids=[mem.id], container_tag=project_tag,
                title=record['objective'][:60], summary_memory_id=mem.id,
            )
            # Phase B: populate typed sub-events from the recorded task data
            # (objective → user_message, each tool call → tool_call, response →
            # agent_message). Importance-gated indexing keeps embedding volume low.
            episodic.events_from_task(
                engine, _ep.id, objective=objective, tool_calls=tool_calls,
                response_text=response_text, container_tag=project_tag, ts=event_date,
            )
        except Exception:
            pass
    except Exception:
        pass
    return record


def get_recent_task_episodes(
    state_dir: Path | str,
    *,
    session_id: str | None = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    rows = _load_episodes(state_dir)
    if session_id:
        rows = [r for r in rows if r.get('session_id') == session_id]
    return rows[-limit:]


def search_execution_memories(
    state_dir: Path | str,
    *,
    query: str,
    project_root: str,
    limit: int = 8,
) -> list[dict[str, Any]]:
    try:
        from memory_engine import MemoryEngine
        engine = MemoryEngine(Path(state_dir))
        project_tag = f"project:{Path(project_root).resolve()}"
        result = engine.recall(query, container_tag=project_tag, limit=limit)
        out: list[dict[str, Any]] = []
        for item in result.memories:
            out.append({
                'content': item.memory.content,
                'score': item.score,
                'source': item.source,
                'memory_id': item.memory.id,
                'category': item.memory.category,
            })
        return out
    except Exception:
        return []
