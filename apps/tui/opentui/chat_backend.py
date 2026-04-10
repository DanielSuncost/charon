#!/usr/bin/env python3
"""Backend process for the OpenTUI chat view.

Runs the ConversationEngine directly (no daemon) and streams events
to the TypeScript frontend via newline-delimited JSON on stdout.

Protocol:
  Frontend → Backend (stdin):
    { "type": "chat", "message": "...", "request_id": "..." }
    { "type": "command", "command": "/setup ...", "request_id": "..." }
    { "type": "refresh", "request_id": "..." }
    { "type": "abort", "request_id": "..." }

  Backend → Frontend (stdout):
    { "type": "chat_delta", "text": "...", "request_id": "..." }
    { "type": "thinking_start", "request_id": "..." }
    { "type": "thinking_delta", "text": "...", "request_id": "..." }
    { "type": "tool_call", "tool_name": "...", "arguments": {...}, "request_id": "..." }
    { "type": "tool_result_delta", "tool_name": "...", "content": "...", "chunk": "...", "request_id": "..." }
    { "type": "tool_result", "tool_name": "...", "content": "...", "is_error": bool, "request_id": "..." }
    { "type": "turn_complete", "request_id": "..." }
    { "type": "chat_complete", "summary": "...", "request_id": "..." }
    { "type": "error", "error": "...", "request_id": "..." }
    { "type": "refresh", "payload": {...}, "request_id": "..." }
    { "type": "status", "message": "...", "request_id": "..." }
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import yaml

# Suppress noisy library output that would corrupt the JSON protocol
os.environ['TRANSFORMERS_VERBOSITY'] = 'error'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))

from provider_bridge import (
    create_provider_and_model,
    resolve_provider_config,
    save_session_provider_config,
    load_session_provider_config,
)
from conversation_engine import ConversationEngine
from tools import ALL_TOOL_DEFS

STATE_DIR = ROOT / '.charon_state'


_emit_lock = threading.Lock()


def emit(event: dict):
    """Send a JSON event to the frontend. Thread-safe."""
    with _emit_lock:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + '\n')
        sys.stdout.flush()


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _boat_registry_path(session_name: str) -> Path:
    name = str(session_name or '').strip()
    if name and not name.startswith('boat-'):
        name = f'boat-{name}'
    return Path.home() / '.charon' / 'boats' / f'{name}.json'


def _boat_socket_for_session(session_name: str) -> str:
    try:
        reg = _load_json(_boat_registry_path(session_name), {})
        return str(reg.get('socket') or '').strip()
    except Exception:
        return ''


def _terminate_boat_session(session_name: str) -> bool:
    name = str(session_name or '').strip()
    if not name:
        return False
    reg_path = _boat_registry_path(name)
    reg = _load_json(reg_path, {}) if reg_path.exists() else {}
    killed = False

    pid = int(reg.get('pid') or 0) if str(reg.get('pid') or '').isdigit() else 0
    if pid > 0:
        try:
            os.kill(pid, 15)
            killed = True
            time.sleep(0.2)
        except ProcessLookupError:
            killed = True
        except Exception:
            pass
        try:
            os.kill(pid, 0)
            try:
                os.kill(pid, 9)
                killed = True
            except Exception:
                pass
        except Exception:
            pass

    socket_path = str(reg.get('socket') or '').strip()
    for candidate in [socket_path, str(reg_path), str(reg_path.with_suffix('.log'))]:
        if not candidate:
            continue
        try:
            Path(candidate).unlink(missing_ok=True)
        except Exception:
            pass
    return killed or reg_path.exists() or bool(socket_path)


def _boat_send_input(session_name: str, text: str) -> bool:
    sock_path = _boat_socket_for_session(session_name)
    if not sock_path:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect(sock_path)
            payload = {
                'type': 'input',
                'data': base64.b64encode(text.encode('utf-8')).decode('ascii'),
            }
            sock.sendall((json.dumps(payload) + '\n').encode('utf-8'))
        return True
    except Exception:
        return False


def _strip_ansi(text: str) -> str:
    return re.sub(r'\x1b\[[0-9;?]*[ -/]*[@-~]', '', text or '')


def _last_visible_line(text: str) -> str:
    cleaned = _strip_ansi(text).replace('\r', '\n')
    parts = [p.strip() for p in cleaned.split('\n') if p.strip()]
    return (parts[-1] if parts else '')[:240]


def _normalize_visible_text(text: str) -> str:
    return ' '.join(_strip_ansi(text).replace('\r', '\n').split()).strip().lower()


def _looks_like_runtime_footer_line(norm: str) -> bool:
    footer_patterns = [
        r'^context usage\b',
        r'^token usage\b',
        r'^usage\b',
        r'^context window\b',
        r'^remaining context\b',
        r'^input tokens\b',
        r'^output tokens\b',
        r'^cached tokens\b',
        r'^reasoning tokens\b',
        r'^total tokens\b',
        r'^latency\b',
        r'^throughput\b',
    ]
    return any(re.search(p, norm) for p in footer_patterns)


def _extract_meaningful_text(output: str, prompt: str = '') -> str:
    cleaned = _strip_ansi(output).replace('\r', '\n')
    prompt_norm = _normalize_visible_text(prompt)
    noise_patterns = [
        r'new message detected',
        r'interrupting',
        r'^\s*thinking\b',
        r'^\s*tool\b',
        r'^\s*bash\b',
        r'^\s*read\b',
        r'^\s*edit\b',
        r'^\s*write\b',
        r'^\s*grep\b',
        r'^\s*sed\b',
        r'^\s*python\b',
        r'^\s*press\s+',
        r'^\s*waiting\s+',
        r'^\s*loading\s+',
        r'^\s*role:\s*(teacher|student)\b',
        r'^\s*(teacher|student)\s+message:\s*$',
        r'^\s*topic:\s+',
        r'type a message',
        r'ctrl\+c',
        r'lm\s*studio',
        r'openai compatible server',
        r'serving model',
        r'^\s*model\s*[:=]',
        r'\b\d+(?:\.\d+)?k/\d+(?:\.\d+)?k\b',
        r'\b\d+(?:\.\d+)?%\b',
        r'\b\d+(?:\.\d+)?s\b',
    ]
    lines = []
    saw_content = False
    for raw in cleaned.split('\n'):
        line = raw.strip()
        if not line:
            continue
        norm = ' '.join(line.split()).strip().lower()
        if not norm:
            continue
        if prompt_norm and (norm in prompt_norm or prompt_norm in norm):
            continue
        if any(re.search(p, norm) for p in noise_patterns):
            continue
        if line.startswith('❯') and ('role:' in norm or 'teacher message:' in norm or 'student message:' in norm):
            continue
        if saw_content and _looks_like_runtime_footer_line(norm):
            break
        if len(re.sub(r'[^A-Za-z0-9]+', '', line)) < 16:
            continue
        if re.fullmatch(r'[-─═━│┃┆┄┈\s]+', line):
            continue
        lines.append(line)
        saw_content = True
    return '\n'.join(lines)[-4000:]


def _boat_capture_output(
    session_name: str,
    timeout: float = 20.0,
    prompt_hint: str = '',
    quiet_period: float = 0.9,
    completion_timeout: float = 10.0,
) -> dict:
    sock_path = _boat_socket_for_session(session_name)
    if not sock_path:
        return {'ok': False, 'error': 'missing socket', 'output': '', 'last_line': ''}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            sock.connect(sock_path)
            reader = sock.makefile('r', encoding='utf-8', errors='ignore')
            sock.sendall((json.dumps({'type': 'subscribe'}) + '\n').encode('utf-8'))
            drain_deadline = time.time() + 0.5
            while time.time() < drain_deadline:
                try:
                    _ = reader.readline()
                except Exception:
                    break

            prompt_norm = _normalize_visible_text(prompt_hint)
            start_deadline = time.time() + timeout
            completion_deadline: float | None = None
            chunks: list[str] = []
            last_line = ''
            meaningful_since: float | None = None
            last_data_at = time.time()
            while True:
                now = time.time()
                if meaningful_since is None and now >= start_deadline:
                    break
                if meaningful_since is not None and completion_deadline is not None and now >= completion_deadline:
                    aggregate = ''.join(chunks)
                    meaningful = _extract_meaningful_text(aggregate, prompt_hint)
                    return {
                        'ok': True,
                        'output': aggregate[-4000:],
                        'last_line': _last_visible_line(meaningful),
                        'message_text': meaningful,
                    }
                try:
                    line = reader.readline()
                except socket.timeout:
                    if meaningful_since is not None and (time.time() - last_data_at) >= quiet_period:
                        aggregate = ''.join(chunks)
                        meaningful = _extract_meaningful_text(aggregate, prompt_hint)
                        return {
                            'ok': True,
                            'output': aggregate[-4000:],
                            'last_line': _last_visible_line(meaningful),
                            'message_text': meaningful,
                        }
                    continue
                except Exception:
                    break
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get('type') != 'output':
                    continue
                try:
                    raw = base64.b64decode(str(msg.get('data') or ''))
                    text_chunk = raw.decode('utf-8', errors='ignore')
                except Exception:
                    text_chunk = ''
                if not text_chunk:
                    continue
                last_data_at = time.time()
                chunks.append(text_chunk)
                candidate = _last_visible_line(text_chunk)
                if candidate:
                    last_line = candidate
                aggregate = ''.join(chunks)
                aggregate_norm = _normalize_visible_text(aggregate)
                last_norm = _normalize_visible_text(last_line)
                is_echo = bool(prompt_norm) and (
                    (last_norm and (last_norm in prompt_norm or prompt_norm in last_norm))
                    or (aggregate_norm and aggregate_norm in prompt_norm)
                )
                meaningful = _extract_meaningful_text(aggregate, prompt_hint)
                has_real_response = bool(meaningful) and not is_echo
                if has_real_response:
                    if meaningful_since is None:
                        meaningful_since = time.time()
                    completion_deadline = time.time() + completion_timeout
            meaningful = _extract_meaningful_text(''.join(chunks), prompt_hint)
            return {
                'ok': False,
                'error': 'timeout waiting for output',
                'output': ''.join(chunks)[-4000:],
                'last_line': _last_visible_line(meaningful),
                'message_text': meaningful,
            }
    except Exception as e:
        return {'ok': False, 'error': str(e), 'output': '', 'last_line': ''}


def _boat_prompt_and_capture(
    session_name: str,
    text: str,
    timeout: float = 20.0,
    quiet_period: float = 0.9,
    completion_timeout: float = 10.0,
) -> dict:
    if not _boat_send_input(session_name, text):
        return {'ok': False, 'error': 'failed to send input', 'output': '', 'last_line': '', 'message_text': ''}
    return _boat_capture_output(
        session_name,
        timeout=timeout,
        prompt_hint=text,
        quiet_period=quiet_period,
        completion_timeout=completion_timeout,
    )


def _wait_for_boat_socket(session_name: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        sock = _boat_socket_for_session(session_name)
        if sock and Path(sock).exists():
            return True
        time.sleep(0.2)
    return False


def _wait_for_boat_ready(session_name: str, timeout: float = 30.0) -> bool:
    sock_path = _boat_socket_for_session(session_name)
    if not sock_path:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(sock_path)
            reader = sock.makefile('r', encoding='utf-8', errors='ignore')
            sock.sendall((json.dumps({'type': 'subscribe'}) + '\n').encode('utf-8'))
            deadline = time.time() + timeout
            observed = ''
            while time.time() < deadline:
                try:
                    line = reader.readline()
                except socket.timeout:
                    continue
                except Exception:
                    break
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get('type') != 'output':
                    continue
                try:
                    raw = base64.b64decode(str(msg.get('data') or ''))
                    chunk = raw.decode('utf-8', errors='ignore')
                except Exception:
                    chunk = ''
                if not chunk:
                    continue
                observed += chunk
                norm = _normalize_visible_text(observed[-4000:])
                if ('type a message' in norm) or ('❯' in observed[-4000:]) or ('hermes' in norm and len(norm) > 20):
                    return True
            return False
    except Exception:
        return False


def _hermes_conversation_runtime_dir(room_id: str, participant_name: str) -> Path:
    rid = re.sub(r'[^a-zA-Z0-9._-]+', '-', str(room_id or '').strip()).strip('-_.') or 'room'
    pname = re.sub(r'[^a-zA-Z0-9._-]+', '-', str(participant_name or '').strip()).strip('-_.') or 'participant'
    return STATE_DIR / 'hermes-conversation-runtime' / rid / pname


def _write_hermes_runtime_home(home: Path, *, model: str, base_url: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    config = {
        'model': {
            'provider': 'custom',
            'base_url': str(base_url or 'http://127.0.0.1:1234/v1').rstrip('/'),
            'default': str(model or 'qwen3-30b-a3b').strip(),
        },
        'toolsets': ['all'],
        'agent': {'max_turns': 60, 'verbose': False, 'reasoning_effort': 'medium'},
        'display': {'compact': False, 'personality': 'helpful'},
        'terminal': {'backend': 'local', 'cwd': str(ROOT), 'timeout': 180},
    }
    (home / 'config.yaml').write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding='utf-8')
    (home / '.env').write_text(
        '\n'.join([
            f'OPENAI_BASE_URL={str(base_url or "http://127.0.0.1:1234/v1").rstrip("/")}',
            'OPENAI_API_KEY=no-key-required',
            f'LLM_MODEL={str(model or "qwen3-30b-a3b").strip()}',
            '',
        ]),
        encoding='utf-8',
    )


def _clean_restored_text(text: str) -> str:
    """Strip leaked thinking tags from persisted assistant content."""
    if not isinstance(text, str) or not text:
        return text
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'(?im)^\s*</?think>\s*$', '', text)
    text = text.replace('<think>', '').replace('</think>', '')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _sanitize_saved_messages(messages: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        item = dict(msg)
        if isinstance(item.get('content'), str):
            item['content'] = _clean_restored_text(item['content'])
        cleaned.append(item)
    return cleaned


def _iso_to_epoch(iso_str: str) -> float:
    """Convert an ISO-8601 timestamp to epoch seconds (best-effort)."""
    if not iso_str:
        return 0.0
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _full_messages_from_store(agent_id: str) -> list | None:
    """Load full raw message list from the lossless SQLite store.

    Returns a list of Message objects, or None if unavailable.
    Used by save paths to ensure JSONL always contains the complete
    history (not compacted engine.messages).
    """
    try:
        from context_store import ContextStore
        from store_adapter import get_db
        from providers import Message as _Msg
        db = get_db(STATE_DIR)
        stored = ContextStore.get_messages_for_agent(db, agent_id, limit=10000)
        if not stored:
            return None
        return [
            _Msg(role=sm.role, content=sm.content,
                 tool_calls=sm.tool_calls,
                 tool_call_id=sm.tool_call_id,
                 tool_name=sm.tool_name,
                 is_error=sm.is_error,
                 thinking=sm.thinking,
                 timestamp=_iso_to_epoch(sm.created_at))
            for sm in stored
        ]
    except Exception:
        return None


def _ui_settings_path() -> Path:
    return STATE_DIR / 'ui_settings.json'


def _load_ui_settings() -> dict:
    return _load_json(_ui_settings_path(), {}) or {}


def _save_ui_settings(settings: dict) -> None:
    path = _ui_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False))


def _projects_registry_path() -> Path:
    return STATE_DIR / 'projects.json'


def _load_project_registry() -> list[dict]:
    data = _load_json(_projects_registry_path(), [])
    return data if isinstance(data, list) else []


def _save_project_registry(projects: list[dict]) -> None:
    path = _projects_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(projects, indent=2, ensure_ascii=False))


def _project_slug(text: str) -> str:
    import re
    slug = re.sub(r'[^a-zA-Z0-9._-]+', '-', str(text or '').strip()).strip('-_.').lower()
    return slug[:96] or 'project'


def _collect_devop_rooms(state_dir: Path, project_root: Path) -> list[dict]:
    rooms = []
    try:
        from devop_runtime import software_ops_root, get_operation_state
        from devop_projection import project_graph, project_f4_stream, summarize_operation

        ops_dir = software_ops_root(state_dir)
        if not ops_dir.exists():
            return []
        wanted_root = str(project_root.resolve())
        for op_path in sorted(ops_dir.glob('*')):
            if not op_path.is_dir():
                continue
            op = get_operation_state(state_dir, op_path.name)
            if not op:
                continue
            op_root = str(op.get('project_root') or '').strip()
            if op_root and op_root != wanted_root:
                continue
            op_id = str(op.get('operation_id') or '').strip()
            if not op_id:
                continue
            graph = project_graph(state_dir, op_id)
            f4 = project_f4_stream(state_dir, op_id)
            summary = summarize_operation(state_dir, op_id)
            rooms.append({
                'id': f'devop-{op_id}',
                'kind': 'software_dev',
                'title': str(op.get('title') or op.get('prompt') or op_id)[:120],
                'project': str(op.get('project_root') or project_root),
                'status': str(op.get('status') or 'active'),
                'created_at': str(op.get('created_at') or ''),
                'updated_at': str(op.get('updated_at') or ''),
                'last_activity': str((summary.get('last_event') or {}).get('ts') or op.get('updated_at') or op.get('created_at') or ''),
                'participants': [
                    {
                        'id': str(n.get('id') or ''),
                        'name': str(n.get('label') or n.get('id') or ''),
                        'role': str(n.get('operation_role') or n.get('node_type') or ''),
                        'status': str(n.get('status') or ''),
                    }
                    for n in (graph.get('nodes') or []) if str(n.get('node_type') or '') == 'agent'
                ],
                'summary': str(op.get('prompt') or '')[:200],
                'operation_id': op_id,
                'domain': 'software_dev',
                'nodes': graph.get('nodes') or [],
                'edges': graph.get('edges') or [],
                'workstreams': f4.get('workstreams') or [],
                'active_reviews': f4.get('active_reviews') or [],
                'events': f4.get('stream') or [],
            })
    except Exception:
        return []
    return rooms


def _dashboard_spark_points(values: list[int], limit: int = 12) -> list[int]:
    vals = [max(0, int(v or 0)) for v in values][-limit:]
    return vals or [0]


def _load_workflow_steps_spec(project_root: Path, raw_value: str) -> list[dict] | None:
    raw = str(raw_value or '').strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        if candidate.exists():
            data = json.loads(candidate.read_text(encoding='utf-8'))
            return data if isinstance(data, list) else None
    except Exception:
        return None
    return None


def _project_goal_tree(state_dir: Path, project_path: str) -> list[dict]:
    try:
        from goal_runtime import list_goals
        goals = list_goals(state_dir, project=project_path)
    except Exception:
        goals = []
    if not goals:
        return []
    by_parent: dict[str, list[dict]] = {}
    roots: list[dict] = []
    for g in goals:
        pid = str(g.get('parent_goal_id') or '')
        by_parent.setdefault(pid, []).append(g)
    def build(node: dict) -> dict:
        gid = str(node.get('goal_id') or '')
        children = [build(c) for c in by_parent.get(gid, [])]
        return {
            'goal_id': gid,
            'title': str(node.get('title') or ''),
            'status': str(node.get('status') or ''),
            'children': children,
        }
    roots = [build(g) for g in by_parent.get('', [])]
    if not roots:
        roots = [build(g) for g in goals[:20]]
    return roots[:20]


def _project_usage_summary(state_dir: Path, project_path: str) -> dict:
    summary = {
        'input_tokens': 0,
        'output_tokens': 0,
        'total_tokens': 0,
        'estimated_cost_usd': 0.0,
        'hours_spent_estimate': 0.0,
        'libris_operations': 0,
        'devop_operations': 0,
    }
    try:
        from libris_runtime import research_root
        rroot = research_root(state_dir, Path(project_path))
        ops_dir = rroot / 'operations'
        if ops_dir.exists():
            for op_path in ops_dir.glob('*'):
                op = _load_json(op_path / 'operation.json', {})
                if not op:
                    continue
                summary['libris_operations'] += 1
                usage = op.get('usage') or {}
                summary['input_tokens'] += int(usage.get('input_tokens') or 0)
                summary['output_tokens'] += int(usage.get('output_tokens') or 0)
                summary['total_tokens'] += int(usage.get('total_tokens') or 0)
                summary['estimated_cost_usd'] += float(usage.get('estimated_cost_usd') or 0.0)
    except Exception:
        pass
    try:
        from devop_runtime import list_operations as _unused  # type: ignore
    except Exception:
        pass
    try:
        from devop_runtime import software_ops_root, get_operation_state
        for op_path in (software_ops_root(state_dir)).glob('*'):
            if not op_path.is_dir():
                continue
            op = get_operation_state(state_dir, op_path.name)
            if not op or str(op.get('project_root') or '').strip() != str(Path(project_path).resolve()):
                continue
            summary['devop_operations'] += 1
            usage = op.get('usage') or {}
            summary['input_tokens'] += int(usage.get('input_tokens') or 0)
            summary['output_tokens'] += int(usage.get('output_tokens') or 0)
            summary['total_tokens'] += int(usage.get('total_tokens') or 0)
            summary['estimated_cost_usd'] += float(usage.get('estimated_cost_usd') or 0.0)
    except Exception:
        pass
    summary['estimated_cost_usd'] = round(float(summary['estimated_cost_usd']), 6)
    summary['hours_spent_estimate'] = round((summary['total_tokens'] / 12000.0), 2) if summary['total_tokens'] else 0.0
    return summary


def _project_activity_points(state_dir: Path, project_path: str) -> list[int]:
    counts = [0] * 12
    try:
        run_log = state_dir / 'run.log'
        if run_log.exists():
            lines = run_log.read_text(encoding='utf-8', errors='replace').splitlines()[-240:]
            for i, _line in enumerate(lines[-12:]):
                counts[min(11, i)] += 1
    except Exception:
        pass
    return _dashboard_spark_points(counts)


def _parse_interval_phrase(text: str) -> int:
    s = str(text or '').strip().lower()
    if not s:
        return 0
    if re.search(r'\bevery\s+hour\b|\bhourly\b', s):
        return 3600
    if re.search(r'\bevery\s+day\b|\bdaily\b', s):
        return 86400
    if re.search(r'\bevery\s+minute\b', s):
        return 60
    m = re.search(r'\bevery\s+(\d+)\s*(minute|minutes|hour|hours|day|days)\b', s)
    if not m:
        return 0
    n = int(m.group(1))
    unit = m.group(2)
    if 'minute' in unit:
        return n * 60
    if 'hour' in unit:
        return n * 3600
    if 'day' in unit:
        return n * 86400
    return 0


def _natural_language_to_cron(text: str) -> str:
    s = str(text or '').strip().lower()
    if not s:
        return ''
    if 'every day at ' in s or 'daily at ' in s:
        m = re.search(r'(?:every day at|daily at)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', s)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            meridiem = (m.group(3) or '').lower()
            if meridiem == 'pm' and hour < 12:
                hour += 12
            if meridiem == 'am' and hour == 12:
                hour = 0
            return f'{minute} {hour} * * *'
    if 'every weekday at ' in s:
        m = re.search(r'every weekday at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', s)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            meridiem = (m.group(3) or '').lower()
            if meridiem == 'pm' and hour < 12:
                hour += 12
            if meridiem == 'am' and hour == 12:
                hour = 0
            return f'{minute} {hour} * * 1-5'
    return ''


class ChatBackend:
    def __init__(self):
        try:
            from automation_scheduler import start_scheduler
            start_scheduler(STATE_DIR, poll_seconds=2.0)
        except Exception:
            pass
        self.engine: ConversationEngine | None = None
        self.chat_history: list[dict] = []
        self._engine_lock = threading.Lock()
        self._active_agent_id: str | None = None
        self.agent_mode: str = 'interactive'  # interactive, autonomous, delegating, idle
        self._notified_batches: set[str] = set()
        self._session_tasks: list[dict] = []
        self._pending_provider_switch: dict | None = None
        self._pending_libris_intake: dict | None = None
        self._pending_remote_onboard: dict | None = None
        self.visible_thoughts: bool = bool(_load_ui_settings().get('visible_thoughts', False))
        self._goal_inference_token_estimate: int = 0
        self._room_runners: set[str] = set()
        self._last_orchestration_parse: dict = {}
        self._owned_boat_sessions: set[str] = set()
        self._shutdown_cleaned = False

    def _register_owned_boat_session(self, session_name: str | None) -> None:
        name = str(session_name or '').strip()
        if name:
            self._owned_boat_sessions.add(name)

    def _cleanup_owned_sessions(self) -> None:
        if self._shutdown_cleaned:
            return
        self._shutdown_cleaned = True
        for session_name in list(self._owned_boat_sessions):
            try:
                _terminate_boat_session(session_name)
            except Exception:
                pass
        self._owned_boat_sessions.clear()

    def _room_runner_mode(self, room: dict | None, fallback: str = 'teacher-student') -> str:
        meta = room.get('meta') if isinstance(room, dict) and isinstance(room.get('meta'), dict) else {}
        raw = str(meta.get('conversation_mode') or meta.get('runner_mode') or fallback or 'teacher-student').strip().lower()
        if raw in ('peer', 'dialogue', 'discuss'):
            return 'peer'
        if raw in ('debate', 'advocate-opposition', 'advocate', 'opposition'):
            return 'debate'
        if raw in ('researcher-reviewer', 'research', 'review'):
            return 'researcher-reviewer'
        if raw in ('pair-programmers', 'pair-programming', 'pair-programmer', 'driver-navigator'):
            return 'pair-programmers'
        if raw in ('strategist-critic', 'strategist/critic', 'strategist', 'critic'):
            return 'strategist-critic'
        if raw in ('planner-critic', 'planner/critic', 'planner critic', 'planner'):
            return 'planner-critic'
        if raw in ('architect-reviewer', 'architect/reviewer', 'architect reviewer', 'architect'):
            return 'architect-reviewer'
        if raw in ('optimist-skeptic', 'optimist/skeptic', 'optimist skeptic', 'optimist', 'skeptic'):
            return 'optimist-skeptic'
        return 'teacher-student'

    def _initial_room_runner_state(self, room: dict | None, topic: str, participants: list[dict], mode: str) -> dict:
        state = {
            'topic': topic,
            'mode': mode,
            'turn': 1,
            'silent_turns': 0,
            'last_utterance': topic,
            'started': False,
            'participants_len': len(participants or []),
        }
        if mode in ('peer', 'debate', 'researcher-reviewer', 'pair-programmers', 'strategist-critic', 'planner-critic', 'architect-reviewer', 'optimist-skeptic'):
            state['current_idx'] = 0
            return state
        teacher = next((p for p in participants if str(p.get('role') or '') == 'teacher'), participants[0] if participants else None)
        state['current_role'] = 'teacher' if teacher else (str(participants[0].get('role') or 'participant') if participants else 'teacher')
        return state

    def _room_control_notes(self, injections: list[dict]) -> str:
        notes = []
        for item in injections or []:
            if not isinstance(item, dict):
                continue
            msg = ' '.join(str(item.get('message') or '').strip().split())
            if not msg:
                continue
            target = str(item.get('target') or 'whole').strip()
            sender = str(item.get('sender') or 'user').strip()
            notes.append(f'- {sender} to [{target}]: {msg}')
        if not notes:
            return ''
        return (
            '\n\nA user interjection has been added to the room. '
            'Treat it as part of the shared conversation state and respond directly to it when relevant.\n'
            + '\n'.join(notes)
        )

    def _room_recent_context(self, room_id: str, limit: int = 8) -> str:
        try:
            from inter_agent_rooms import list_events
            events = list_events(STATE_DIR, room_id, limit=limit * 3)
        except Exception:
            events = []
        lines: list[str] = []
        for ev in events[-limit:]:
            if not isinstance(ev, dict):
                continue
            et = str(ev.get('type') or '')
            if et == 'participant_output':
                who = str(ev.get('speaker_role') or ev.get('participant') or 'participant').strip() or 'participant'
                text = ' '.join(str(ev.get('text') or ev.get('summary') or '').split()).strip()
                if text:
                    lines.append(f'{who}: {text[:300]}')
            elif et in ('room_message_sent', 'room_injection_queued', 'room_injection_requested'):
                target = str(ev.get('target') or 'whole').strip() or 'whole'
                text = ' '.join(str(ev.get('message') or ev.get('summary') or '').split()).strip()
                if text:
                    lines.append(f'user→{target}: {text[:300]}')
        if not lines:
            return ''
        return '\n\nRecent room transcript:\n' + '\n'.join(f'- {line}' for line in lines[-limit:])

    def _build_room_turn_prompt(self, *, room_id: str, mode: str, topic: str, state: dict, participants: list[dict], injections: list[dict]) -> tuple[str, str, str, str]:
        notes = self._room_control_notes(injections)
        recent_context = self._room_recent_context(room_id)
        turn = int(state.get('turn') or 1)
        last_utterance = str(state.get('last_utterance') or topic).strip() or topic

        if mode in ('peer', 'debate', 'researcher-reviewer', 'pair-programmers', 'strategist-critic', 'planner-critic', 'architect-reviewer', 'optimist-skeptic'):
            current_idx = int(state.get('current_idx') or 0)
            default_role_pairs = {
                'peer': ('peer-1', 'peer-2'),
                'debate': ('advocate', 'opposition'),
                'researcher-reviewer': ('researcher', 'reviewer'),
                'pair-programmers': ('driver', 'navigator'),
                'strategist-critic': ('strategist', 'critic'),
                'planner-critic': ('planner', 'critic'),
                'architect-reviewer': ('architect', 'reviewer'),
                'optimist-skeptic': ('optimist', 'skeptic'),
            }
            default_roles = default_role_pairs.get(mode, ('peer-1', 'peer-2'))
            if mode == 'peer' and len(participants) > 2:
                speakers = [
                    (
                        str(p.get('name') or f'Hermes {idx + 1}'),
                        str(p.get('session') or ''),
                        str(p.get('role') or f'peer-{idx + 1}'),
                    )
                    for idx, p in enumerate(participants)
                ]
                if not speakers:
                    return '', '', '', ''
                cur_name, cur_session, cur_role = speakers[current_idx % len(speakers)]
                previous_idx = (current_idx - 1) % len(speakers)
                other_name, _, _ = speakers[previous_idx]
                if turn == 1:
                    prompt = (
                        f"You are {cur_name} in a live multi-agent peer conversation about: {topic}. "
                        "Open with one useful perspective, framing question, or distinction that helps the group think better. "
                        "Keep it natural and concise."
                        f"{recent_context}{notes}\r"
                    )
                else:
                    prompt = (
                        f"You are {cur_name} in a live multi-agent peer conversation about: {topic}. "
                        f"The latest message in the conversation came from {other_name}. Respond naturally to that point below, while helping advance the overall group discussion. "
                        "You may agree, challenge, refine, or redirect, but stay concise and conversational.\n\n"
                        f"Latest message:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            p1 = participants[0] if len(participants) > 0 else {}
            p2 = participants[1] if len(participants) > 1 else {}
            speakers = [
                (str(p1.get('name') or 'Hermes 1'), str(p1.get('session') or ''), str(p1.get('role') or default_roles[0])),
                (str(p2.get('name') or 'Hermes 2'), str(p2.get('session') or ''), str(p2.get('role') or default_roles[1])),
            ]
            cur_name, cur_session, cur_role = speakers[current_idx]
            other_name, _, other_role = speakers[1 - current_idx]
            if mode == 'debate':
                if turn == 1:
                    prompt = (
                        f"You are {cur_name}, taking the {cur_role} role in a live debate with {other_name} about: {topic}. "
                        "Open with your strongest concise case. State your position clearly and press one key consideration."
                        f"{recent_context}{notes}\r"
                    )
                else:
                    prompt = (
                        f"You are {cur_name}, taking the {cur_role} role in a live debate with {other_name} about: {topic}. "
                        f"Respond directly to {other_name}'s latest point from the {other_role} side below. Refute weak assumptions, concede only if warranted, and sharpen your case. "
                        "Keep it pointed and concise.\n\n"
                        f"{other_name} said:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            if mode == 'researcher-reviewer':
                if cur_role == 'researcher':
                    if turn == 1:
                        prompt = (
                            f"You are {cur_name}, the researcher, in a live research discussion with {other_name}, the reviewer, about: {topic}. "
                            "Open with a hypothesis, framing, or evidence-based angle worth exploring. Keep it rigorous but concise."
                            f"{recent_context}{notes}\r"
                        )
                    else:
                        prompt = (
                            f"You are {cur_name}, the researcher, continuing a live research discussion with {other_name}, the reviewer, about: {topic}. "
                            "Respond to the reviewer's latest critique below. Clarify assumptions, strengthen the argument, and note what evidence would matter most.\n\n"
                            f"Reviewer said:\n{last_utterance}"
                            f"{recent_context}{notes}\r"
                        )
                else:
                    prompt = (
                        f"You are {cur_name}, the reviewer, in a live research discussion with {other_name}, the researcher, about: {topic}. "
                        "Respond to the latest point below by testing assumptions, evidence quality, scope, or missing considerations. Be constructive but rigorous.\n\n"
                        f"Researcher said:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            if mode == 'pair-programmers':
                if cur_role == 'driver':
                    if turn == 1:
                        prompt = (
                            f"You are {cur_name}, the driver, in a live pair-programming conversation with {other_name}, the navigator, about this task: {topic}. "
                            "Open by clarifying the goal, proposing a concrete implementation slice, and naming the first step. Keep it practical and concise."
                            f"{recent_context}{notes}\r"
                        )
                    else:
                        prompt = (
                            f"You are {cur_name}, the driver, continuing a live pair-programming conversation with {other_name}, the navigator, about: {topic}. "
                            "Respond to the navigator's latest guidance below. Move the implementation forward, make tradeoffs explicit, and keep the thread concrete.\n\n"
                            f"Navigator said:\n{last_utterance}"
                            f"{recent_context}{notes}\r"
                        )
                else:
                    prompt = (
                        f"You are {cur_name}, the navigator, in a live pair-programming conversation with {other_name}, the driver, about: {topic}. "
                        "Respond to the driver's latest point below by checking approach, risks, tests, edge cases, and next steps. Be concrete and helpful.\n\n"
                        f"Driver said:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            if mode == 'strategist-critic':
                if cur_role == 'strategist':
                    if turn == 1:
                        prompt = (
                            f"You are {cur_name}, the strategist, in a live strategist/critic conversation with {other_name}, the critic, about: {topic}. "
                            "Open with a concrete strategic framing, propose a promising direction, and explain why it seems leverageful. Be substantive rather than slogan-like."
                            f"{recent_context}{notes}\r"
                        )
                    else:
                        prompt = (
                            f"You are {cur_name}, the strategist, continuing a live strategist/critic conversation with {other_name}, the critic, about: {topic}. "
                            "Respond to the critic's latest challenge below. Refine the plan, clarify priorities, resolve tradeoffs, and preserve a strong actionable through-line.\n\n"
                            f"Critic said:\n{last_utterance}"
                            f"{recent_context}{notes}\r"
                        )
                else:
                    prompt = (
                        f"You are {cur_name}, the critic, in a live strategist/critic conversation with {other_name}, the strategist, about: {topic}. "
                        "Respond to the strategist's latest point below by stress-testing assumptions, spotting failure modes, identifying blind spots, and pushing for sharper reasoning. Be rigorous but constructive.\n\n"
                        f"Strategist said:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            if mode == 'planner-critic':
                if cur_role == 'planner':
                    if turn == 1:
                        prompt = (
                            f"You are {cur_name}, the planner, in a live planner/critic conversation with {other_name}, the critic, about: {topic}. "
                            "Open with a concrete plan, sequencing, milestones, and major dependencies. Focus on execution realism and actionable next steps."
                            f"{recent_context}{notes}\r"
                        )
                    else:
                        prompt = (
                            f"You are {cur_name}, the planner, continuing a live planner/critic conversation with {other_name}, the critic, about: {topic}. "
                            "Respond to the critic's latest challenge below by revising the plan, clarifying sequencing, tightening scope, and making tradeoffs explicit.\n\n"
                            f"Critic said:\n{last_utterance}"
                            f"{recent_context}{notes}\r"
                        )
                else:
                    prompt = (
                        f"You are {cur_name}, the critic, in a live planner/critic conversation with {other_name}, the planner, about: {topic}. "
                        "Respond to the planner's latest point below by identifying unrealistic assumptions, missing dependencies, hidden complexity, schedule risk, or unclear ownership. Be tough but constructive.\n\n"
                        f"Planner said:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            if mode == 'architect-reviewer':
                if cur_role == 'architect':
                    if turn == 1:
                        prompt = (
                            f"You are {cur_name}, the architect, in a live architect/reviewer conversation with {other_name}, the reviewer, about: {topic}. "
                            "Open with a concrete architecture, design rationale, interfaces, and key tradeoffs. Aim for a thoughtful systems-level proposal."
                            f"{recent_context}{notes}\r"
                        )
                    else:
                        prompt = (
                            f"You are {cur_name}, the architect, continuing a live architect/reviewer conversation with {other_name}, the reviewer, about: {topic}. "
                            "Respond to the reviewer's latest critique below by defending or revising the design, clarifying constraints, and making the architecture more coherent.\n\n"
                            f"Reviewer said:\n{last_utterance}"
                            f"{recent_context}{notes}\r"
                        )
                else:
                    prompt = (
                        f"You are {cur_name}, the reviewer, in a live architect/reviewer conversation with {other_name}, the architect, about: {topic}. "
                        "Respond to the architect's latest point below by probing design weaknesses, unclear interfaces, operability concerns, failure modes, and maintainability issues. Be rigorous and concrete.\n\n"
                        f"Architect said:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            if mode == 'optimist-skeptic':
                if cur_role == 'optimist':
                    if turn == 1:
                        prompt = (
                            f"You are {cur_name}, the optimist, in a live optimist/skeptic conversation with {other_name}, the skeptic, about: {topic}. "
                            "Open by making the strongest case for why this could work out well, what upside is underappreciated, and what enabling factors matter most. Be specific, not gushy."
                            f"{recent_context}{notes}\r"
                        )
                    else:
                        prompt = (
                            f"You are {cur_name}, the optimist, continuing a live optimist/skeptic conversation with {other_name}, the skeptic, about: {topic}. "
                            "Respond to the skeptic's latest point below by addressing the concern, refining the upside case, and distinguishing real risk from over-pessimism.\n\n"
                            f"Skeptic said:\n{last_utterance}"
                            f"{recent_context}{notes}\r"
                        )
                else:
                    prompt = (
                        f"You are {cur_name}, the skeptic, in a live optimist/skeptic conversation with {other_name}, the optimist, about: {topic}. "
                        "Respond to the optimist's latest point below by identifying risks, weak assumptions, downside scenarios, and reasons the upside case may be overstated. Be sharp but fair.\n\n"
                        f"Optimist said:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            if turn == 1:
                prompt = (
                    f"You are {cur_name} in a live peer conversation with {other_name} about: {topic}. "
                    "Open with your initial perspective, framing question, or useful distinction. "
                    "Aim for a natural, thoughtful, substantive response rather than a rigid format or just one or two sentences."
                    f"{recent_context}{notes}\r"
                )
            else:
                prompt = (
                    f"You are {cur_name} continuing a live peer conversation with {other_name} about: {topic}. "
                    "Respond naturally to the latest point below. You can agree, challenge, refine, or redirect. "
                    "Keep it thoughtful and substantive; a few solid paragraphs or a well-developed short response is better than a throwaway answer.\n\n"
                    f"{other_name} said:\n{last_utterance}"
                    f"{recent_context}{notes}\r"
                )
            return prompt, cur_role, cur_session, cur_name

        current_role = str(state.get('current_role') or 'teacher')
        teacher = next((p for p in participants if str(p.get('role') or '') == 'teacher'), participants[0] if participants else {})
        student = next((p for p in participants if str(p.get('role') or '') == 'student'), participants[1] if len(participants) > 1 else {})
        teacher_session = str(teacher.get('session') or '')
        student_session = str(student.get('session') or '')
        if current_role == 'teacher':
            if turn == 1:
                prompt = (
                    f"You are the teacher in a live teaching conversation about: {topic}. "
                    "Start naturally. Prioritize clarity, pacing, and concrete understanding over roleplay tropes. "
                    "A short substantive opening is better than a scripted one."
                    f"{recent_context}{notes}\r"
                )
            else:
                prompt = (
                    f"You are the teacher in an ongoing conversation about: {topic}. "
                    "Reply naturally to the student's latest message below. Clarify, extend, or gently correct as needed. "
                    "Ask a follow-up only if it genuinely helps the conversation move.\n\n"
                    f"Student message:\n{last_utterance}"
                    f"{recent_context}{notes}\r"
                )
            return prompt, 'teacher', teacher_session, str(teacher.get('name') or 'Teacher')

        prompt = (
            f"You are the student in an ongoing conversation about: {topic}. "
            "Reply naturally to the teacher's latest message below. Share what made sense, what remains unclear, and where you want to go next. "
            "A question is welcome but not mandatory every turn.\n\n"
            f"Teacher message:\n{last_utterance}"
            f"{recent_context}{notes}\r"
        )
        return prompt, 'student', student_session, str(student.get('name') or 'Student')

    def _advance_room_runner_state(self, state: dict, mode: str, utterance: str) -> dict:
        next_state = dict(state or {})
        next_state['last_utterance'] = utterance
        next_state['silent_turns'] = 0
        next_state['turn'] = int(next_state.get('turn') or 1) + 1
        next_state['started'] = True
        if mode in ('peer', 'debate', 'researcher-reviewer', 'pair-programmers', 'strategist-critic', 'planner-critic', 'architect-reviewer', 'optimist-skeptic'):
            participants_len = max(2, int(next_state.get('participants_len') or 2))
            if mode == 'peer' and participants_len > 2:
                next_state['current_idx'] = (int(next_state.get('current_idx') or 0) + 1) % participants_len
            else:
                next_state['current_idx'] = 1 - int(next_state.get('current_idx') or 0)
        else:
            next_state['current_role'] = 'student' if str(next_state.get('current_role') or 'teacher') == 'teacher' else 'teacher'
        return next_state

    def _start_conversation_room_runner(self, room_id: str, topic: str, participants: list[dict], mode: str = 'teacher-student') -> None:
        rid = str(room_id or '').strip()
        if not rid or rid in self._room_runners:
            return
        self._room_runners.add(rid)

        def _run() -> None:
            try:
                from inter_agent_rooms import (
                    append_event,
                    consume_injections,
                    load_room,
                    load_runner_state,
                    save_runner_state,
                    update_room,
                )

                room = load_room(STATE_DIR, rid)
                if not room:
                    return
                mode_local = self._room_runner_mode(room, mode)
                room_participants = list(room.get('participants') or participants or [])
                topic_local = str(room.get('title') or topic or '').strip() or topic
                state = load_runner_state(STATE_DIR, rid) or self._initial_room_runner_state(room, topic_local, room_participants, mode_local)
                save_runner_state(STATE_DIR, rid, state)

                from conversation_runtime import runtime_for_participant

                if mode_local in ('peer', 'debate', 'researcher-reviewer', 'pair-programmers', 'strategist-critic', 'planner-critic', 'architect-reviewer', 'optimist-skeptic'):
                    if len(room_participants) < 2:
                        append_event(STATE_DIR, rid, {'type': 'runner_error', 'message': 'need at least 2 participants for this conversation'})
                        return
                    for participant in room_participants:
                        runtime = runtime_for_participant(participant)
                        if not runtime.wait_until_ready(timeout=15.0):
                            append_event(STATE_DIR, rid, {'type': 'runner_error', 'message': f"participant runtime not ready: {participant.get('name') or participant.get('role') or participant.get('session') or 'unknown'}"})
                            return
                else:
                    teacher = next((p for p in room_participants if str(p.get('role') or '') == 'teacher'), room_participants[0] if room_participants else None)
                    student = next((p for p in room_participants if str(p.get('role') or '') == 'student'), room_participants[1] if len(room_participants) > 1 else None)
                    if not teacher or not student:
                        append_event(STATE_DIR, rid, {'type': 'runner_error', 'message': 'missing teacher/student participants'})
                        return
                    if not runtime_for_participant(teacher).wait_until_ready(timeout=15.0) or not runtime_for_participant(student).wait_until_ready(timeout=15.0):
                        append_event(STATE_DIR, rid, {'type': 'runner_error', 'message': 'participant runtime not ready'})
                        return

                if not bool(state.get('started')):
                    time.sleep(6.0)
                    append_event(STATE_DIR, rid, {
                        'type': 'conversation_started',
                        'topic': topic_local,
                        'mode': (mode_local if mode_local in ('peer', 'debate', 'researcher-reviewer', 'pair-programmers', 'strategist-critic', 'planner-critic', 'architect-reviewer', 'optimist-skeptic') else 'relay'),
                    })
                    emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})

                while True:
                    room = load_room(STATE_DIR, rid)
                    if not room:
                        break
                    room_status = str(room.get('status') or 'active')
                    if room_status in ('stopped', 'deleted'):
                        break
                    if room_status == 'paused':
                        time.sleep(0.5)
                        continue

                    room_participants = list(room.get('participants') or room_participants or [])
                    state = load_runner_state(STATE_DIR, rid) or state
                    prompt, speaker_role, speaker_session, speaker_name = self._build_room_turn_prompt(
                        room_id=rid,
                        mode=mode_local,
                        topic=topic_local,
                        state=state,
                        participants=room_participants,
                        injections=consume_injections(
                            STATE_DIR,
                            rid,
                            speaker_role=(
                                str(room_participants[int(state.get('current_idx') or 0)].get('role') or 'philosopher-1')
                                if mode_local == 'dialogue' and len(room_participants) > 0
                                else str(state.get('current_role') or 'teacher')
                            ),
                            participant=(
                                room_participants[int(state.get('current_idx') or 0)]
                                if mode_local == 'dialogue' and len(room_participants) > 0
                                else next((p for p in room_participants if str(p.get('role') or '') == str(state.get('current_role') or 'teacher')), room_participants[0] if room_participants else None)
                            ),
                        ),
                    )
                    if not speaker_session:
                        append_event(STATE_DIR, rid, {'type': 'runner_error', 'message': f'missing session for {speaker_role}'})
                        break

                    turn = int(state.get('turn') or 1)
                    append_event(STATE_DIR, rid, {
                        'type': 'conversation_turn_started',
                        'turn': turn,
                        'speaker_role': speaker_role,
                        'session': speaker_session,
                        'summary': prompt.splitlines()[0][:200],
                    })
                    update_room(STATE_DIR, rid, summary=f'{speaker_name} turn {turn}: {topic_local}', active_speaker=speaker_role, active_state='thinking')
                    emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})

                    participant_count = len(room_participants)
                    response_start_timeout = 12.0 if participant_count >= 3 else 16.0
                    quiet_period = 0.5 if participant_count >= 3 else 0.7
                    completion_timeout = 8.0 if participant_count >= 3 else 12.0
                    speaker_participant = next((p for p in room_participants if str(p.get('session') or '') == speaker_session), None)
                    runtime = runtime_for_participant(speaker_participant or {'session': speaker_session})
                    turn_runtime = {
                        'phase': 'thinking',
                        'research_started_at': None,
                        'reply_started': False,
                        'nudge_sent': False,
                    }
                    research_nudge_after = 8.0 if participant_count <= 2 else 6.0

                    def _runtime_event(evt: dict) -> None:
                        et = str((evt or {}).get('type') or '')
                        now = time.time()
                        if et == 'tool_progress':
                            if turn_runtime['research_started_at'] is None:
                                turn_runtime['research_started_at'] = now
                            turn_runtime['phase'] = 'researching'
                            summary = str((evt or {}).get("summary") or "tool activity")[:160]
                            tool_name = str((evt or {}).get('tool_name') or '').strip()
                            tool_phase = str((evt or {}).get('tool_phase') or '').strip()
                            if tool_name or tool_phase:
                                append_event(STATE_DIR, rid, {
                                    'type': 'participant_tool_progress',
                                    'turn': turn,
                                    'speaker_role': speaker_role,
                                    'session': speaker_session,
                                    'tool_name': tool_name,
                                    'tool_phase': tool_phase,
                                    'summary': summary,
                                })
                            update_room(
                                STATE_DIR,
                                rid,
                                summary=f'{speaker_name} researching: {summary}',
                                active_speaker=speaker_role,
                                active_state='researching',
                            )
                            if (not turn_runtime['reply_started'] and not turn_runtime['nudge_sent'] and turn_runtime['research_started_at'] is not None and (now - float(turn_runtime['research_started_at'] or now)) >= research_nudge_after):
                                if runtime.send_input('Please reply to the conversation now with what you have so far. If research is incomplete, briefly note the uncertainty and continue.'):
                                    turn_runtime['nudge_sent'] = True
                                    append_event(STATE_DIR, rid, {
                                        'type': 'turn_nudged',
                                        'turn': turn,
                                        'speaker_role': speaker_role,
                                        'session': speaker_session,
                                        'summary': f'{speaker_name} was nudged to answer after extended research',
                                    })
                            emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})
                        elif et in ('reply_started', 'reply_progress'):
                            text = str((evt or {}).get('text') or '').strip()
                            if text:
                                turn_runtime['reply_started'] = True
                                turn_runtime['phase'] = 'replying'
                                update_room(
                                    STATE_DIR,
                                    rid,
                                    summary=f'{speaker_name} drafting reply: {text[:160]}',
                                    active_speaker=speaker_role,
                                    active_state='replying',
                                )
                                emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})

                    result = runtime.prompt_and_capture(
                        prompt,
                        timeout=response_start_timeout,
                        quiet_period=quiet_period,
                        completion_timeout=completion_timeout,
                        on_event=_runtime_event,
                    ).as_dict()
                    utterance = str(result.get('message_text') or '').strip() or str(result.get('last_line') or '').strip()
                    if utterance:
                        utterance = utterance[:8000]
                        state = self._advance_room_runner_state(state, mode_local, utterance)
                        save_runner_state(STATE_DIR, rid, state)
                        append_event(STATE_DIR, rid, {
                            'type': 'participant_output',
                            'turn': turn,
                            'speaker_role': speaker_role,
                            'session': speaker_session,
                            'summary': utterance[:240],
                            'text': utterance,
                            'last_line': str(result.get('last_line') or '')[:240],
                        })
                        update_room(STATE_DIR, rid, summary=f'{speaker_name} replied on turn {turn}', active_speaker=speaker_role, active_state='handoff')
                        emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})
                        time.sleep(0.15)
                        continue

                    captured = False
                    for wait_idx in range(2):
                        append_event(STATE_DIR, rid, {
                            'type': 'turn_waiting',
                            'turn': turn,
                            'speaker_role': speaker_role,
                            'session': speaker_session,
                            'summary': f"still waiting for response{'' if wait_idx == 0 else ' (retry listen)'}",
                        })
                        update_room(STATE_DIR, rid, summary=f'{speaker_name} waiting for visible reply', active_speaker=speaker_role, active_state='waiting')
                        emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})
                        result = runtime.capture_output(
                            timeout=(6.0 if participant_count >= 3 else 10.0),
                            prompt_hint=prompt,
                            quiet_period=(0.5 if participant_count >= 3 else 0.7),
                            completion_timeout=(6.0 if participant_count >= 3 else 10.0),
                            on_event=_runtime_event,
                        ).as_dict()
                        utterance = str(result.get('message_text') or '').strip() or str(result.get('last_line') or '').strip()
                        if not utterance:
                            continue
                        utterance = utterance[:8000]
                        state = self._advance_room_runner_state(state, mode_local, utterance)
                        save_runner_state(STATE_DIR, rid, state)
                        append_event(STATE_DIR, rid, {
                            'type': 'participant_output',
                            'turn': turn,
                            'speaker_role': speaker_role,
                            'session': speaker_session,
                            'summary': utterance[:240],
                            'text': utterance,
                            'last_line': str(result.get('last_line') or '')[:240],
                        })
                        update_room(STATE_DIR, rid, summary=f'{speaker_name} replied on turn {turn}', active_speaker=speaker_role, active_state='handoff')
                        emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})
                        time.sleep(0.15)
                        captured = True
                        break
                    if captured:
                        continue

                    silent_turns = int(state.get('silent_turns') or 0) + 1
                    state['silent_turns'] = silent_turns
                    save_runner_state(STATE_DIR, rid, state)
                    append_event(STATE_DIR, rid, {
                        'type': 'turn_timeout',
                        'turn': turn,
                        'speaker_role': speaker_role,
                        'session': speaker_session,
                        'summary': str(result.get('error') or 'no visible output')[:240],
                    })
                    update_room(STATE_DIR, rid, summary=f'{speaker_name} stalled on turn {turn}', active_speaker=speaker_role, active_state='stalled')
                    emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})
                    if silent_turns >= 3:
                        append_event(STATE_DIR, rid, {'type': 'conversation_stalled', 'turn': turn, 'speaker_role': speaker_role, 'topic': topic_local})
                        update_room(STATE_DIR, rid, status='paused', summary=f'Paused after repeated timeouts on turn {turn}', active_speaker=speaker_role, active_state='paused')
                        break
                    time.sleep(0.5 if participant_count >= 3 else 1.0)

                room = load_room(STATE_DIR, rid)
                if room and str(room.get('status') or '') != 'paused':
                    append_event(STATE_DIR, rid, {'type': 'conversation_stopped', 'topic': topic_local, 'turns_completed': max(0, int((load_runner_state(STATE_DIR, rid) or {}).get('turn') or 1) - 1)})
                    emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})
            finally:
                self._room_runners.discard(rid)

        threading.Thread(target=_run, daemon=True).start()

    def _conversation_role_prompt(self, agent_display: str, role: str, title: str, room_id: str, runner_mode: str) -> str:
        if runner_mode in ('peer', 'dialogue'):
            return (
                f'You are {agent_display} in an ongoing peer conversation about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Use your own judgment about tone, depth, and pacing. '
                'Stay thoughtful, concise, and ready to continue from incoming prompts.'
            )
        if runner_mode == 'debate':
            return (
                f'You are {agent_display} in an ongoing debate about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Take a clear side, respond directly, and keep your arguments sharp but concise.'
            )
        if runner_mode == 'researcher-reviewer':
            return (
                f'You are {agent_display} in an ongoing researcher/reviewer conversation about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Stay rigorous, evidence-oriented, and concise.'
            )
        if runner_mode == 'pair-programmers':
            return (
                f'You are {agent_display} in an ongoing pair-programming conversation about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Stay practical, concrete, and focused on implementation progress.'
            )
        if runner_mode == 'strategist-critic':
            return (
                f'You are {agent_display} in an ongoing strategist/critic conversation about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Aim for substantive strategic reasoning, clear tradeoffs, and constructive pressure-testing rather than shallow one-liners.'
            )
        if runner_mode == 'planner-critic':
            return (
                f'You are {agent_display} in an ongoing planner/critic conversation about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Focus on execution realism, sequencing, dependencies, and constructive challenge rather than shallow one-liners.'
            )
        if runner_mode == 'architect-reviewer':
            return (
                f'You are {agent_display} in an ongoing architect/reviewer conversation about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Aim for thoughtful systems reasoning, concrete interfaces, and rigorous design review.'
            )
        if runner_mode == 'optimist-skeptic':
            return (
                f'You are {agent_display} in an ongoing optimist/skeptic conversation about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Explore upside and downside concretely, with fair but substantive argument rather than caricature.'
            )
        return (
            f'You are {agent_display} in room {room_id} about: {title}. '
            f'Your role is {role}. '
            'Charon handles turn-taking and may occasionally inject steering. '
            'Keep a natural conversational voice, stay responsive to the current turn, and avoid overfitting to rigid phrasing rules.'
        )

    def _create_conversation_room(
        self,
        *,
        agent_type: str,
        kind: str,
        title: str,
        project: str,
        participants: list[dict],
        meta: dict,
        request_id: str | None,
        start_runner: bool,
        runner_mode: str = 'teacher-student',
    ) -> dict:
        from inter_agent_rooms import create_room, append_event, slugify, update_room
        from conversation_participants import get_conversation_adapter
        import subprocess as _sp

        adapter = get_conversation_adapter(agent_type)
        if not adapter:
            raise RuntimeError(f'Unsupported conversation agent type: {agent_type}')
        if not adapter.capabilities.can_spawn:
            raise RuntimeError(f'Conversation spawning not wired yet for agent type: {agent_type}')

        room_meta = dict(meta or {})
        room_meta.setdefault('provider', agent_type)
        room_meta.setdefault('agent_type', agent_type)
        room = create_room(
            STATE_DIR,
            kind=kind,
            title=title,
            project=project,
            participants=participants,
            meta=room_meta,
        )
        append_event(STATE_DIR, room['id'], {
            'type': f'{kind}_requested',
            'provider': agent_type,
            'count': len(participants),
            'topic': title,
        })

        room_slug = slugify(title)
        room_id_slug = slugify(str(room.get('id') or room_slug))
        room_short = room_id_slug[-6:] if len(room_id_slug) >= 6 else room_id_slug
        title_short = room_slug[:18].strip('-_.') or 'conversation'
        launched = []
        bound_participants = []
        runtime_status = None
        for idx, participant_seed in enumerate(participants):
            agent_name = f'hc-{title_short}-{room_short}-{agent_type[:2]}{idx+1}'
            role = str(participant_seed.get('role') or 'participant')
            agent_display = participant_seed.get('name') or f'{adapter.display_name} {idx+1}'
            role_prompt = self._conversation_role_prompt(agent_display, role, title, str(room.get('id') or ''), runner_mode)
            child_env = os.environ.copy()
            if agent_type == 'hermes':
                hermes_runtime = self._conversation_hermes_local_runtime(room_meta)
                runtime_home = _hermes_conversation_runtime_dir(room['id'], agent_name)
                _write_hermes_runtime_home(
                    runtime_home,
                    model=str(hermes_runtime.get('model') or 'qwen3-30b-a3b'),
                    base_url=str(hermes_runtime.get('base_url') or 'http://127.0.0.1:1234/v1'),
                )
                child_env['HERMES_HOME'] = str(runtime_home)
                child_env['HERMES_INFERENCE_PROVIDER'] = 'custom'
                child_env['OPENAI_BASE_URL'] = str(hermes_runtime.get('base_url') or 'http://127.0.0.1:1234/v1')
                child_env['OPENAI_API_KEY'] = 'no-key-required'
                child_env['HERMES_MODEL'] = str(hermes_runtime.get('model') or 'qwen3-30b-a3b')
                child_env['LLM_MODEL'] = str(hermes_runtime.get('model') or 'qwen3-30b-a3b')
                child_env.pop('OPENROUTER_API_KEY', None)
                child_env.pop('OPENROUTER_BASE_URL', None)
                runtime_status = hermes_runtime
            cmd = adapter.spawn_command(project_root=ROOT, session_name=agent_name, participant=adapter.build_participant(participant_seed))
            _sp.Popen(cmd, cwd=str(ROOT), env=child_env, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            session_name = f'boat-{agent_name}'
            self._register_owned_boat_session(session_name)
            launched.append(agent_name)
            participant = dict(participant_seed)
            participant['session'] = session_name
            participant['agent_type'] = agent_type
            bound_participants.append(participant)
            append_event(STATE_DIR, room['id'], {
                'type': 'participant_spawned',
                'participant': participant.get('name') or f'{adapter.display_name} {idx+1}',
                'role': role,
                'session': session_name,
                'agent_type': agent_type,
                'prompt': role_prompt[:200],
            })
        room = update_room(STATE_DIR, room['id'], participants=bound_participants, participant_sessions=[p.get('session') for p in bound_participants]) or room
        from conversation_runtime import runtime_for_participant
        for participant in bound_participants:
            runtime_for_participant(participant).wait_until_ready(timeout=15.0)
        if start_runner and len(bound_participants) >= 2:
            self._start_conversation_room_runner(room['id'], title, bound_participants, mode=runner_mode)
        emit({'type': 'status', 'message': f'Created {adapter.display_name} {kind} room: {room.get("title", title)} ({room["id"]})', 'request_id': request_id})
        if runtime_status:
            emit({'type': 'status', 'message': f'{adapter.display_name} conversation runtime forced local: {runtime_status.get("model", "qwen3-30b-a3b")} @ {runtime_status.get("base_url", "http://127.0.0.1:1234/v1")}', 'request_id': request_id})
        emit({'type': 'status', 'message': f'Launched wrapped {adapter.display_name} sessions: ' + ', '.join(launched), 'request_id': request_id})
        emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
        return room

    def _outcomes_path(self, session_id: str | None = None) -> Path | None:
        sid = session_id or self._active_agent_id
        if not sid:
            return None
        return STATE_DIR / 'conversations' / f'{sid}.outcomes.json'

    def _save_session_outcomes(self) -> None:
        path = self._outcomes_path()
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._session_tasks, indent=2, ensure_ascii=False))
        except Exception:
            pass

    def _is_ack_message(self, text: str) -> bool:
        t = text.strip().lower()
        if not t:
            return False
        return bool(re.match(
            r'^(ok|okay|thanks|thank you|looks good|great|nice|cool|yep|yes|approved|perfect|that works|sounds good)[!. ]*$',
            t,
        ))

    def _starts_with_ack(self, text: str) -> bool:
        """True if message begins with an ack word followed by more content (e.g. 'okay, do X next')."""
        t = text.strip().lower()
        return bool(re.match(
            r'^(ok|okay|thanks|great|nice|cool|yep|yes|perfect|sounds good)[!.,\s]',
            t,
        ))

    def _is_redirect_message(self, text: str) -> bool:
        t = text.strip().lower()
        if not t:
            return False
        patterns = [
            r'\bno\b', r'\bnot what i meant\b', r'\bthat\'s wrong\b',
            r'\bwhy are you\b', r'\bwhy aren\'t you\b', r'\binstead\b',
            r'\bactually\b', r'\bi meant\b', r'\bchange what\b',
            r'\bwhat happened\b', r'\btry again\b', r'\bthat\'s not\b',
        ]
        return any(re.search(p, t) for p in patterns)

    def _parse_intent(self, text: str) -> tuple[str, str] | None:
        t = ' '.join(text.strip().lower().split())
        if not t or self._is_ack_message(t):
            return None
        t = re.sub(r'^(please\s+|can you\s+|could you\s+|would you\s+|help me\s+|i want you to\s+|let\'s\s+|lets\s+)', '', t)
        mappings = [
            ('fixed', [r'^fix\s+', r'^repair\s+']),
            ('implemented', [r'^implement\s+', r'^add\s+', r'^create\s+', r'^build\s+']),
            ('updated', [r'^update\s+', r'^change\s+', r'^adjust\s+', r'^rename\s+']),
            ('investigated', [r'^investigate\s+', r'^diagnose\s+', r'^debug\s+', r'^look into\s+', r'^figure out\s+', r'^why\b', r'^what happened\b']),
            ('researched', [r'^research\s+', r'^explore\s+', r'^review\s+', r'^inspect\s+']),
            ('prevented', [r'^prevent\s+', r'^block\s+', r'^stop\s+']),
            ('refactored', [r'^refactor\s+', r'^clean up\s+']),
        ]
        for kind, pats in mappings:
            for pat in pats:
                if re.search(pat, t):
                    obj = re.sub(pat, '', t).strip(' .?!')
                    obj = re.sub(r'^(the|a|an)\s+', '', obj)
                    obj = obj[:80] if obj else 'task'
                    return kind, obj
        return None

    def _make_outcome_title(self, kind: str, obj: str, status: str) -> str:
        if status == 'active':
            active_map = {
                'fixed': 'fixing', 'implemented': 'implementing', 'updated': 'updating',
                'investigated': 'investigating', 'researched': 'researching',
                'prevented': 'preventing', 'refactored': 'refactoring',
            }
            return f"{active_map.get(kind, 'working on')} {obj}".strip()
        return f'{kind} {obj}'.strip()

    def _clean_orchestration_topic(self, topic: str) -> str:
        cleaned = ' '.join(str(topic or '').strip().split())
        if not cleaned:
            return ''
        cleaned = re.sub(r'^[\s,:;.-]+', '', cleaned)
        cleaned = re.sub(r'^(?:a|an|the)\s+', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\b(?:and\s+)?the\s+conversation\s+continues?.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\buntil\s+i\s+stop\s+it.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\bto\s+(?:the\s+)?other\b.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\bto\s+(?:a|another)\s+(?:student|agent|hermes\s+agent|learner|beginner)\b.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'^(?:another|the\s+other)\s+(?:on|about|for)\s+', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\b(?:who|that)\s+wants?\s+to\s+understand\b.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\bone\s+is\s+the\s+(?:teacher|tutor|expert|mentor)\b.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\bthe\s+other\s+is\s+the\s+(?:student|learner|beginner)\b.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\b(?:please\s+)?keep\s+going\b.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\bbetween\s+two\s+hermes\s+agents\b.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\bwith\s+two\s+hermes\s+agents\b.*$', '', cleaned, flags=re.I)
        cleaned = cleaned.strip(' .,!?:;-')
        if cleaned.lower() in {'this', 'that', 'it'}:
            return ''
        return cleaned

    def _normalize_orchestration_request_text(self, text: str) -> str:
        normalized = ' '.join(str(text or '').strip().split())
        normalized = re.sub(r'^(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|i\s+want\s+|i\s+want\s+you\s+to\s+|let\'s\s+|lets\s+)', '', normalized, flags=re.I)
        normalized = re.sub(r'^(?:i\'d\s+like|i\s+would\s+like|we\s+need)\s+', 'start ', normalized, flags=re.I)
        normalized = re.sub(r'\bset\s+up\b', 'start', normalized, flags=re.I)
        normalized = re.sub(r'\bhave\b', 'start', normalized, flags=re.I)
        normalized = re.sub(r'\bget\b', 'start', normalized, flags=re.I)
        return normalized

    def _extract_orchestration_topic_from_text(self, text: str) -> str:
        stripped = ' '.join(str(text or '').strip().split())
        if not stripped:
            return ''
        patterns = [
            r'\b(?:explaining|explain|teaching|teach(?:ing)?|mentoring|mentor(?:ing)?|tutoring|tutor(?:ing)?|walking\s+through|walk\s+through)\s+(.+?)(?:[.?!]|$)',
            r'\b(?:pair\s*program(?:ming)?|implement(?:ing)?|debug(?:ging)?|refactor(?:ing)?)\s+(.+?)(?:[.?!]|$)',
            r'\b(?:about|on|for)\s+(.+?)(?:[.?!]|$)',
            r'\b(?:discuss(?:ing)?|talk(?:ing)?\s+about|chat(?:ting)?\s+about|brainstorm(?:ing)?|debate|debating|riff(?:ing)?\s+on)\s+(.+?)(?:[.?!]|$)',
        ]
        for pat in patterns:
            match = re.search(pat, stripped, re.I)
            if match:
                topic = self._clean_orchestration_topic(match.group(1))
                if topic:
                    return topic
        return ''

    def _last_user_chat_message(self) -> str:
        for msg in reversed(self.chat_history):
            if str(msg.get('role') or '') != 'user':
                continue
            content = str(msg.get('content') or '').strip()
            if not content or content.startswith('/'):
                continue
            return content
        return ''

    def _infer_orchestration_topic(self, text: str, context_text: str = '') -> str:
        direct = self._extract_orchestration_topic_from_text(text)
        if direct:
            return direct
        stripped = ' '.join(str(text or '').strip().split())
        if re.search(r'\b(?:about|on|for|discuss|talk\s+about|chat\s+about|explain|walk\s+through)\s+(?:this|that|it)\b', stripped, re.I):
            context_topic = self._extract_orchestration_topic_from_text(context_text)
            if context_topic:
                return context_topic
            return self._clean_orchestration_topic(context_text)[:160]
        return ''

    def _detect_orchestration_provider(self, lower: str) -> str:
        from conversation_participants import supported_conversation_agent_types
        candidates = supported_conversation_agent_types()
        for provider in candidates:
            if re.search(rf'\b{re.escape(provider)}\b', lower):
                return provider
        return 'hermes'

    def _parse_orchestration_json(self, text: str) -> dict | None:
        raw = str(text or '').strip()
        if not raw:
            return None
        candidates = [raw]
        fenced = re.findall(r'```(?:json)?\s*(\{.*?\})\s*```', raw, flags=re.S | re.I)
        candidates.extend(fenced)
        brace_match = re.search(r'(\{.*\})', raw, flags=re.S)
        if brace_match:
            candidates.append(brace_match.group(1))
        for candidate in candidates:
            try:
                data = json.loads(candidate)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
        return None

    def _should_prefer_llm_orchestration(self, text: str) -> bool:
        lower = ' '.join(str(text or '').strip().lower().split())
        if not lower:
            return False
        ambiguous_patterns = [
            r'\bone\s+agent\b.+\b(other|another)\s+agent\b',
            r'\bone\s+.*\bthe\s+other\s+.*',
            r'\bplan\b.+\bcritic',
            r'\bcritique\b.+\bplan',
            r'\barchitecture\s+review\b',
            r'\bdesign\s+review\b',
            r'\bbest\s+case\b.+\bworst\s+case\b',
            r'\boptimistic\b.+\bskeptical\b',
            r'\bstrategic\b.+\bcritic',
            r'\bproposes?\b.+\breviews?\b',
        ]
        return any(re.search(p, lower) for p in ambiguous_patterns)

    def _llm_fallback_orchestration_command(self, text: str) -> tuple[str, str] | None:
        lower = ' '.join(str(text or '').strip().lower().split())
        if not lower:
            return None
        if not re.search(r'\b(start|create|spawn|launch|open|begin|orchestrate|conversation|team|agents?|participants?|room|session)\b', lower):
            return None
        try:
            from conversation_participants import supported_conversation_agent_types
            from model_registry import get_shade_provider_and_model
            from conversation_engine import ConversationEngine
        except Exception:
            return None

        provider, model, ready = get_shade_provider_and_model(STATE_DIR, phase_name='analysis', task_complexity='normal')
        if not ready:
            return None

        supported = supported_conversation_agent_types()
        archetypes = [
            'peer', 'teacher-student', 'debate', 'researcher-reviewer',
            'pair-programmers', 'strategist-critic', 'planner-critic',
            'architect-reviewer', 'optimist-skeptic',
        ]
        system_prompt = (
            'You are a lightweight orchestration intent parser for Charon. '
            'Return exactly one JSON object and nothing else. '
            'Do not use tools. Do not explain your reasoning. '
            'Infer the best conversation command intent from the user request. '
            'If the request is not clearly about creating a conversation/team room, return '
            '{"intent":"none","confidence":0}. '
            'Valid intents: conversation, team, none. '
            f'Valid providers: {", ".join(supported)}. '
            f'Valid archetypes: {", ".join(archetypes)}. '
            'Use count >= 2. Use team for 3+ generic multi-agent conversations. '
            'For two-agent archetypes use intent=conversation. '
            'JSON schema: '
            '{"intent":"conversation|team|none","provider":"...","count":2,'
            '"archetype":"...","topic":"...","confidence":0.0}.'
        )
        prompt = f'User request: {text.strip()}\nReturn JSON only.'
        try:
            engine = ConversationEngine(
                provider=provider,
                model=model,
                project_root=ROOT,
                agent_name='shades-router',
                system_prompt=system_prompt,
                state_dir=STATE_DIR,
                max_turns=1,
                max_tool_calls_per_turn=0,
                max_tokens=600,
            )
            reply, _events = asyncio.run(engine.submit_and_collect(prompt))
        except Exception:
            return None

        data = self._parse_orchestration_json(reply)
        if not isinstance(data, dict):
            return None
        intent = str(data.get('intent') or '').strip().lower()
        if intent not in ('conversation', 'team'):
            return None
        confidence = float(data.get('confidence') or 0.0)
        if confidence < 0.55:
            return None
        provider_name = str(data.get('provider') or '').strip().lower() or self._detect_orchestration_provider(lower)
        if provider_name not in supported:
            return None
        count_raw = data.get('count')
        try:
            count = max(2, int(count_raw or 2))
        except Exception:
            count = 2
        archetype = str(data.get('archetype') or '').strip().lower().replace('/', '-').replace(' ', '-')
        topic = self._clean_orchestration_topic(str(data.get('topic') or self._infer_orchestration_topic(text, self._last_user_chat_message()) or 'open discussion'))
        if not topic:
            topic = 'open discussion'

        alias_map = {
            'teacher-student': ('conversation', 'teacher student'),
            'peer': ('conversation', 'peer'),
            'debate': ('conversation', 'debate'),
            'researcher-reviewer': ('conversation', 'researcher reviewer'),
            'pair-programmers': ('conversation', 'pair-programmers'),
            'strategist-critic': ('conversation', 'strategist critic'),
            'planner-critic': ('conversation', 'planner critic'),
            'architect-reviewer': ('conversation', 'architect reviewer'),
            'optimist-skeptic': ('conversation', 'optimist skeptic'),
        }
        if intent == 'team' or count > 2:
            cmd = f'/team {provider_name} {count} {topic}'
            return cmd, f'Routing orchestration request via shades parser to {cmd}'
        if archetype in alias_map:
            _kind, cmd_suffix = alias_map[archetype]
            cmd = f'/conversation {provider_name} {cmd_suffix} {topic}'
            return cmd, f'Routing orchestration request via shades parser to {cmd}'
        if count == 2:
            cmd = f'/conversation {provider_name} peer {topic}'
            return cmd, f'Routing orchestration request via shades parser to {cmd}'
        cmd = f'/team {provider_name} {count} {topic}'
        return cmd, f'Routing orchestration request via shades parser to {cmd}'

    def _match_nl_orchestration_command(self, text: str) -> tuple[str, str] | None:
        context_text = self._last_user_chat_message()
        stripped = self._normalize_orchestration_request_text(text)
        lower = stripped.lower()
        if not stripped:
            return None
        provider = self._detect_orchestration_provider(lower)
        if provider not in lower:
            return None

        action_requested = bool(re.search(r'\b(make|create|spawn|start|launch|open|begin|setup|set\s+up|orchestrate)\b', lower))
        roomish_request = bool(re.search(r'\b(room|rooms|conversation|conversations|chat|chats|dialogue|discussion|discussions|team|teams|session|sessions|agent|agents|participant|participants|exchange)\b', lower))
        roleish_request = bool(re.search(r'\b(teacher|student|tutor|learner|beginner|expert|mentor)\b', lower))
        if not action_requested or not (roomish_request or roleish_request):
            return None

        count = None
        count_match = re.search(r'\b(\d+)\b', lower)
        if count_match:
            count = max(2, int(count_match.group(1)))
        else:
            word_to_num = {'two': 2, 'three': 3, 'four': 4, 'five': 5}
            for word, value in word_to_num.items():
                if re.search(rf'\b{word}\b', lower):
                    count = value
                    break

        teacher_student_hint = (
            ('teacher' in lower and 'student' in lower)
            or any(token in lower for token in [' tutor ', ' learner ', ' beginner ', ' expert ', ' mentor '])
            or bool(re.search(r'\bteacher\s+explain', lower))
            or bool(re.search(r'\bone\s+is\s+the\s+(?:teacher|tutor|expert|mentor)\b', lower))
            or bool(re.search(r'\bthe\s+other\s+is\s+the\s+(?:student|learner|beginner)\b', lower))
            or bool(re.search(r'\b(?:explaining|teaching|mentoring|tutoring)\b.+\b(?:student|learner|beginner)\b', lower))
            or bool(re.search(r'\b(?:expert|teacher|tutor|mentor)\b.+\b(?:beginner|learner|student)\b', lower))
        )
        prefer_llm = self._should_prefer_llm_orchestration(stripped)
        debate_hint = bool(re.search(r'\b(debate|argue|argument|arguing|for\s+and\s+against|pros\s+and\s+cons|advocate|opposition)\b', lower))
        pair_programming_hint = bool(re.search(r'\b(pair\s*program(?:ming)?|pairing|driver|navigator|implement(?:ing)?|debug(?:ging)?|refactor(?:ing)?|coding)\b', lower))
        researcher_reviewer_hint = bool(re.search(r'\b(researcher\s+and\s+reviewer|researcher/reviewer|research\s+and\s+review|review\s+my\s+research|research\s+question)\b', lower))
        strategist_critic_hint = bool(re.search(r'\b(strategist\s+and\s+critic|strategist/critic|strategy\s+and\s+critique|stress[- ]?test\s+the\s+strategy)\b', lower))
        planner_critic_hint = bool(re.search(r'\b(planner\s+and\s+critic|planner/critic|planning\s+and\s+critique|execution\s+plan)\b', lower)) or bool(re.search(r'\bone\s+agent\b.+\bplan(?:s|ning)?\b.+\b(other|another)\s+agent\b.+\bcritic|critique\b', lower))
        architect_reviewer_hint = bool(re.search(r'\b(architect\s+and\s+reviewer|architect/reviewer|architecture\s+review|design\s+review)\b', lower)) or bool(re.search(r'\bone\s+agent\b.+\barchitect(?:ure)?\b.+\b(other|another)\s+agent\b.+\breview\b', lower))
        optimist_skeptic_hint = bool(re.search(r'\b(optimist\s+and\s+skeptic|optimist/skeptic|optimism\s+and\s+skepticism|best\s+case\s+and\s+worst\s+case)\b', lower)) or (('optimistic' in lower or 'optimist' in lower) and ('skeptical' in lower or 'skeptic' in lower))
        topic = self._infer_orchestration_topic(stripped, context_text) or 'open discussion'

        if prefer_llm:
            llm_route = self._llm_fallback_orchestration_command(text)
            if llm_route:
                return llm_route

        if teacher_student_hint:
            return (
                f'/conversation {provider} teacher student {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} teacher student {topic}',
            )

        if debate_hint:
            return (
                f'/conversation {provider} debate {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} debate {topic}',
            )

        if researcher_reviewer_hint:
            return (
                f'/conversation {provider} researcher reviewer {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} researcher reviewer {topic}',
            )

        if strategist_critic_hint:
            return (
                f'/conversation {provider} strategist critic {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} strategist critic {topic}',
            )

        if planner_critic_hint:
            return (
                f'/conversation {provider} planner critic {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} planner critic {topic}',
            )

        if architect_reviewer_hint:
            return (
                f'/conversation {provider} architect reviewer {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} architect reviewer {topic}',
            )

        if optimist_skeptic_hint:
            return (
                f'/conversation {provider} optimist skeptic {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} optimist skeptic {topic}',
            )

        if pair_programming_hint:
            return (
                f'/conversation {provider} pair-programmers {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} pair-programmers {topic}',
            )

        if count == 2:
            return (
                f'/conversation {provider} peer {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} peer {topic}',
            )

        if count and count > 2:
            return (
                f'/team {provider} {count} {topic}',
                f'Routing orchestration request via fast-path to /team {provider} {count} {topic}',
            )

        if re.search(r'\b(team|room|session|sessions|agent|agents|participant|participants)\b', lower):
            return (
                f'/conversation {provider} peer {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} peer {topic}',
            )

        generic_team_match = re.match(
            rf'^(?:make|create|spawn|start|launch|open|begin|orchestrate)\s+(?:me\s+)?(?:a\s+)?(?:(\d+)\s+)?(?:charons[- ]boat\s+wrapped\s+)?{re.escape(provider)}\s+(?:sessions?|agents?|team|participants?)\s*(?:and\s+have\s+them\s+(?:discuss|chat|talk|brainstorm|debate|riff)(?:\s+back\s+and\s+forth)?\s+|to\s+(?:discuss|chat|talk|brainstorm|debate|riff)\s+|for\s+|about\s+|on\s+)?(.+)?$',
            stripped,
            re.I,
        )
        if generic_team_match:
            fallback_count = int(generic_team_match.group(1) or count or 2)
            fallback_topic = self._clean_orchestration_topic(generic_team_match.group(2) or topic) or self._infer_orchestration_topic(stripped, context_text) or 'open discussion'
            if fallback_count <= 2:
                return (
                    f'/conversation {provider} peer {fallback_topic}',
                    f'Routing orchestration request via fast-path to /conversation {provider} peer {fallback_topic}',
                )
            return (
                f'/team {provider} {fallback_count} {fallback_topic}',
                f'Routing orchestration request via fast-path to /team {provider} {fallback_count} {fallback_topic}',
            )

        return self._llm_fallback_orchestration_command(text)

    def _resolve_pending_outcome(self, status: str) -> None:
        if not self._session_tasks:
            return
        for item in reversed(self._session_tasks):
            if item.get('status') == 'active':
                item['status'] = status
                item['resolved_at'] = time.time()
                if not str(item.get('title') or '').strip():
                    item['title'] = self._make_outcome_title(item.get('kind', 'updated'), item.get('object', 'task'), status)
                self._save_session_outcomes()
                return

    def _is_question_message(self, text: str) -> bool:
        """True if the message is a question or lookup request (no clear action to track as an outcome)."""
        t = ' '.join(text.strip().lower().split())
        if not t:
            return False
        # Pure acks are handled separately
        if self._is_ack_message(t):
            return True
        # Strip leading ack phrase so "okay, so what are..." → "what are..."
        t = re.sub(r'^(ok|okay|thanks|great|nice|cool|yep|yes|perfect|sounds good)[!.,\s]+(so\s+)?', '', t)
        question_patterns = [
            r'^what\b', r'^where\b', r'^when\b', r'^who\b', r'^which\b', r'^how\b',
            r'^show me\b', r'^list\b', r'^tell me\b', r'^can you show\b',
            r'^can you list\b', r'^can you tell\b', r'^do you\b', r'^is there\b',
            r'^are there\b', r'^give me\b', r'^what\'s\b', r'^whats\b',
            r'^any more\b', r'^more\b', r'^what about\b', r'^anything\b',
        ]
        return any(re.match(p, t) for p in question_patterns)

    def _start_outcome_for_message(self, message: str) -> None:
        parsed = self._parse_intent(message)
        if parsed:
            kind, obj = parsed
            title = self._make_outcome_title(kind, obj, 'active')
        elif self._is_question_message(message):
            # Questions and lookups don't produce a trackable outcome — skip.
            return
        else:
            kind, obj = 'working', 'task'
            try:
                from task_summarizer import summarize_instruction_fast
                title = summarize_instruction_fast(message)
            except Exception:
                title = message[:80].strip() or 'Working on task'
        self._session_tasks.append({
            'task_id': f'outcome-{int(time.time() * 1000)}',
            'status': 'active',
            'kind': kind,
            'object': obj,
            'title': title,
            'instruction': message[:200],
            'summary': '',
            'detail': '',
            'tokens_in': 0,
            'tokens_out': 0,
            'tool_calls': 0,
            'turns': 0,
            'files_touched': [],
            'ts': time.time(),
            'resolved_at': 0,
        })
        self._save_session_outcomes()

    def _improve_active_outcome_title_background(self, message: str, request_id: str | None) -> None:
        if not self._session_tasks:
            return
        task_id = str(self._session_tasks[-1].get('task_id') or '')
        if not task_id:
            return

        def _run() -> None:
            try:
                import asyncio as _aio
                from task_summarizer import summarize_instruction_rich, summarize_instruction_fast
                try:
                    from model_registry import get_shade_provider_and_model, load_registry
                    reg = load_registry(STATE_DIR)
                    provider, model, ready = get_shade_provider_and_model(STATE_DIR, reg=reg)
                except Exception:
                    provider = model = None
                    ready = False
                if ready and provider is not None and model is not None:
                    title = _aio.run(summarize_instruction_rich(
                        instruction=message,
                        provider=provider,
                        model=model,
                    ))
                else:
                    title = summarize_instruction_fast(message)
                title = str(title or '').strip()[:100]
                if not title:
                    return
                for item in reversed(self._session_tasks):
                    if str(item.get('task_id') or '') != task_id:
                        continue
                    if item.get('status') != 'active':
                        return
                    item['title'] = title
                    item['detail'] = f'Task: {message[:160]}'
                    item['ts'] = time.time()
                    self._save_session_outcomes()
                    emit({'type': 'refresh', 'payload': {'session_info': self._get_session_info()}, 'request_id': request_id})
                    return
            except Exception:
                return

        threading.Thread(target=_run, daemon=True).start()

    def _load_tasks_from_ledger(self, agent_id: str | None = None) -> None:
        """Load session-local outcome ledger for the current conversation.

        This is intentionally session-scoped. We only restore outcomes when
        resuming a specific session, not from agent-level memory.
        """
        path = self._outcomes_path(agent_id)
        if not path or not path.exists():
            self._session_tasks = []
            return
        try:
            data = json.loads(path.read_text())
            self._session_tasks = data if isinstance(data, list) else []
        except Exception:
            self._session_tasks = []

    def _ensure_engine(self) -> tuple[ConversationEngine | None, str]:
        """Create or return the conversation engine.
        Returns (engine, error_message).
        """
        # Register approval callback so tool calls can ask for permission
        try:
            from tools import set_approval_callback
            def _emit_approval(tool_name, params_summary, risk, reason):
                emit({
                    'type': 'approval_request',
                    'tool': tool_name,
                    'params': params_summary,
                    'risk': risk,
                    'reason': reason,
                })
            set_approval_callback(_emit_approval)
        except Exception:
            pass

        if self.engine is not None:
            return self.engine, ''

        self._ensure_session_id()

        try:
            provider, model, ready = create_provider_and_model(STATE_DIR, self._active_agent_id)
        except Exception as e:
            return None, f'Provider setup failed: {e}'

        if not ready:
            return None, 'No provider configured. Use /setup provider <name> to configure.'

        project = str(ROOT)
        onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
        configured_project = str(onboarding.get('project') or '').strip()
        if configured_project:
            project = configured_project

        # Build enriched system prompt with memory, goals, coordination.
        # Fresh launches should stay fresh: do NOT silently bind to an existing
        # persistent agent unless the user explicitly requested one via
        # CHARON_AGENT / --agent.
        system_prompt = ''
        try:
            from system_prompt_builder import build_system_prompt as build_layered_prompt
            agent_info = {'id': '', 'name': 'Charon', 'role': 'charon', 'goal': '', 'project': project}
            requested_agent = os.environ.get('CHARON_AGENT', '').strip()
            self._bound_agent_id = None
            if requested_agent:
                try:
                    from agent_lifecycle import list_agents
                    for a in list_agents():
                        if a.get('id') == requested_agent or a.get('name') == requested_agent:
                            agent_info = a
                            self._bound_agent_id = a.get('id') or None
                            break
                except Exception:
                    pass
            task_info = {'project': project}
            system_prompt = build_layered_prompt(
                state_dir=STATE_DIR, agent=agent_info, task=task_info,
            )
        except Exception as e:
            import traceback
            sys.stderr.write(f'System prompt builder failed: {e}\n')
            traceback.print_exc(file=sys.stderr)

        # Session ID is created before provider resolution so provider selection can be session-scoped.
        self._ensure_session_id()

        # Use an explicitly bound persistent agent only when requested.
        # Otherwise the engine runs as this fresh session's own identity.
        engine_agent_id = getattr(self, '_bound_agent_id', None) or self._active_agent_id
        self.engine = ConversationEngine(
            provider=provider,
            model=model,
            project_root=project,
            agent_id=engine_agent_id,
            agent_name='Charon',
            system_prompt=system_prompt,
            state_dir=STATE_DIR,
            max_tokens=32768,
        )

        # Apply provider handoff transfer if present.
        try:
            from context_transfer import load_pending_transfer, apply_transfer_to_engine, clear_pending_transfer, record_transfer_event
            pending_transfer = load_pending_transfer(STATE_DIR)
            if pending_transfer:
                apply_transfer_to_engine(self.engine, pending_transfer)
                clear_pending_transfer(STATE_DIR)
                record_transfer_event(STATE_DIR, {
                    'ts': pending_transfer.get('created_at', ''),
                    'type': 'transfer_applied',
                    'bundle_id': pending_transfer.get('id', ''),
                    'source_provider': pending_transfer.get('source', {}).get('provider', ''),
                    'target_provider': pending_transfer.get('target', {}).get('provider', ''),
                    'session_id': pending_transfer.get('source', {}).get('session_id', ''),
                })
                emit({
                    'type': 'status',
                    'message': f'Applied context transfer {pending_transfer.get("id", "")}. Session continued on new provider.',
                })
        except Exception:
            pass

        # Session registration deferred until first message is sent
        # (don't clutter the session list with empty sessions)

        # Only resume when explicitly requested via --resume flag or /resume command
        if self._active_agent_id and os.environ.get('CHARON_RESUME', '').strip():
            try:
                saved = None
                aid = self._active_agent_id

                # Try lossless store first — query by agent_id directly
                store_msgs = _full_messages_from_store(aid)
                if store_msgs:
                    if self.engine:
                        self.engine.messages = list(store_msgs)
                    from conversation_store import message_to_dict
                    saved = [message_to_dict(m) for m in store_msgs]
                else:
                    # Fall back to JSONL
                    from conversation_store import load_conversation, dict_to_message
                    saved = _sanitize_saved_messages(load_conversation(STATE_DIR, aid))
                    if saved and self.engine:
                        msgs = [dict_to_message(m) for m in saved]
                        self.engine.messages = msgs
                        # Migrate JSONL messages into lossless store for future resumes
                        if self.engine.has_lossless_store:
                            self.engine.import_into_store(msgs)

                if saved:
                    self._load_tasks_from_ledger(aid)
                    emit({
                        'type': 'conversation_restored',
                        'messages': saved,
                        'count': len(saved),
                        'agent_id': aid,
                    })
            except Exception:
                pass

        return self.engine, ''

    def _get_refresh_payload(self) -> dict:
        onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
        session_id = self._active_agent_id or None
        session_override = load_session_provider_config(STATE_DIR, session_id) if session_id else {}
        effective_onboarding = dict(onboarding)
        if session_override:
            effective_onboarding.update(session_override)

        session_cfg = self._session_provider_state()
        provider = str(session_cfg.get('provider_raw') or effective_onboarding.get('provider') or '').strip()
        model = str(session_cfg.get('model_id') or effective_onboarding.get('model') or effective_onboarding.get('provider_model') or '').strip()
        complete = bool(session_cfg.get('ready') or effective_onboarding.get('complete'))
        if self.engine is not None:
            provider = str(getattr(self.engine, 'provider_name', '') or provider).strip()
            model = str(getattr(getattr(self.engine, 'model', None), 'model_id', '') or model).strip()
            complete = True

        # Load agents
        agents = []
        try:
            from agent_lifecycle import list_agents
            for a in list_agents():
                agent_id = a.get('id', '')
                # Load recent actions from inbox
                recent_actions = []
                inbox_path = STATE_DIR / 'agents' / agent_id / 'inbox.jsonl'
                if inbox_path.exists():
                    try:
                        inbox_lines = inbox_path.read_text().splitlines()[-8:]
                        for line in inbox_lines:
                            try:
                                rec = json.loads(line)
                                evt = rec.get('event_type', '')
                                payload = rec.get('payload', {})
                                summary = payload.get('summary', payload.get('instruction', ''))
                                if summary:
                                    recent_actions.append(f"{evt}: {str(summary)[:60]}")
                                elif evt:
                                    recent_actions.append(evt)
                            except Exception:
                                pass
                    except Exception:
                        pass

                # Load working memory for goal info
                memory_path = STATE_DIR / 'agents' / agent_id / 'working_memory.json'
                memory = _load_json(memory_path, {})
                last_summary = memory.get('last_task_summary', '')
                notes = memory.get('notes', [])

                agents.append({
                    'id': agent_id,
                    'name': a.get('name', ''),
                    'status': a.get('status', 'idle'),
                    'role': a.get('role', 'charon'),
                    'goal': a.get('goal', ''),
                    'specialization': a.get('specialization', ''),
                    'project': a.get('project', ''),
                    'mode': a.get('mode', 'persistent'),
                    'visibility': a.get('visibility', 'user'),
                    'last_active': a.get('last_active', ''),
                    'parent_agent_id': a.get('parent_agent_id', ''),
                    'tmux_session': a.get('tmux_session', ''),
                    'recent_actions': recent_actions,
                    'last_summary': str(last_summary)[:120] if last_summary else '',
                    'memory_notes': len(notes),
                })

                # Add ledger entries for rear-view
                try:
                    from task_ledger import get_agent_ledger
                    ledger = get_agent_ledger(STATE_DIR, agent_id, limit=10)
                    agents[-1]['ledger'] = ledger
                except Exception:
                    agents[-1]['ledger'] = []

                # Add shade usage stats
                try:
                    from shade_stats import get_agent_shade_stats
                    agents[-1]['shade_stats'] = get_agent_shade_stats(STATE_DIR, agent_id)
                except Exception:
                    agents[-1]['shade_stats'] = {}
        except Exception:
            pass

        # Remote fleet agents
        try:
            from fleet_registry import load_fleet
            from fleet_sync import get_cached_fleet_status
            fleet = load_fleet()
            fleet_status = get_cached_fleet_status()
            for server in fleet.get('servers', []):
                server_id = server.get('id', server.get('host', ''))
                server_info = fleet_status.get(server_id, {})
                server_sessions = server_info.get('sessions', {})
                for agent_cfg in server.get('agents', []):
                    agent_name = agent_cfg.get('name', '')
                    session_info = server_sessions.get(agent_name, {})
                    remote_status = session_info.get('status', 'offline') if server_info.get('online') else 'offline'
                    agents.append({
                        'id': f"remote:{server_id}:{agent_name}",
                        'name': agent_name,
                        'status': remote_status,
                        'role': agent_cfg.get('type', 'remote'),
                        'goal': '',
                        'specialization': agent_cfg.get('specialization', ''),
                        'project': agent_cfg.get('project', ''),
                        'mode': 'persistent',
                        'visibility': 'user',
                        'last_active': '',
                        'parent_agent_id': '',
                        'tmux_session': session_info.get('session_id', ''),
                        'recent_actions': [],
                        'last_summary': '',
                        'memory_notes': 0,
                        'is_remote': True,
                        'server_id': server_id,
                        'host': server.get('host', ''),
                        'transport': 'remote-boat',
                    })
        except Exception:
            pass

        # Derive projects from agents
        project_map: dict[str, dict] = {}
        for a in agents:
            proj = a.get('project', '').strip()
            if not proj:
                continue
            name = proj.split('/')[-1] or proj
            if name not in project_map:
                project_map[name] = {
                    'name': name,
                    'path': proj,
                    'agents': [],
                    'agent_details': [],
                    'last_active': '',
                    'started': '',
                }
            project_map[name]['agents'].append(a.get('name', a.get('id', '')))
            project_map[name]['agent_details'].append({
                'name': a.get('name', ''),
                'id': a.get('id', ''),
                'status': a.get('status', 'idle'),
                'role': a.get('role', 'charon'),
            })
            ts = a.get('last_active', '')
            if ts > project_map[name].get('last_active', ''):
                project_map[name]['last_active'] = ts
            created = a.get('created_at', '')
            if not project_map[name]['started'] or (created and created < project_map[name]['started']):
                project_map[name]['started'] = created
        projects = list(project_map.values())

        # Merge explicit project objects from registry
        try:
            registry = _load_project_registry()
            by_name = {p.get('name', ''): p for p in projects}
            for entry in registry:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get('name') or '').strip()
                path = str(entry.get('path') or '').strip()
                if not name:
                    continue
                proj = by_name.get(name)
                if proj is None:
                    proj = {
                        'name': name,
                        'path': path,
                        'agents': [],
                        'agent_details': [],
                        'last_active': '',
                        'started': str(entry.get('created_at') or ''),
                        'active': False,
                        'explicit': True,
                        'description': str(entry.get('description') or ''),
                    }
                    projects.append(proj)
                    by_name[name] = proj
                else:
                    proj['explicit'] = True
                    if path and not proj.get('path'):
                        proj['path'] = path
                    if entry.get('description') and not proj.get('description'):
                        proj['description'] = str(entry.get('description') or '')
            onboarding_project = str(onboarding.get('project') or '').strip()
            for p in projects:
                p['active'] = any(ad.get('status') == 'running' for ad in p.get('agent_details', []))
                p['selected'] = bool(onboarding_project and str(p.get('path') or '').strip() == onboarding_project)
        except Exception:
            for p in projects:
                p['active'] = any(ad.get('status') == 'running' for ad in p.get('agent_details', []))

        for p in projects:
            path = str(p.get('path') or '').strip()
            usage = _project_usage_summary(STATE_DIR, path or str(ROOT))
            goal_tree = _project_goal_tree(STATE_DIR, path or str(ROOT))
            flat_goals = []
            try:
                from goal_runtime import list_goals
                flat_goals = list_goals(STATE_DIR, project=path or str(ROOT))
            except Exception:
                flat_goals = []
            p['usage'] = usage
            p['goal_tree'] = goal_tree
            p['goal_counts'] = {
                'total': len(flat_goals),
                'completed': sum(1 for g in flat_goals if str(g.get('status') or '') == 'completed'),
                'active': sum(1 for g in flat_goals if str(g.get('status') or '') in {'active', 'executing', 'planning', 'verifying'}),
                'pending': sum(1 for g in flat_goals if str(g.get('status') or '') in {'backlog', 'proposed', 'confirmed'}),
                'blocked': sum(1 for g in flat_goals if str(g.get('status') or '') == 'blocked'),
            }
            p['activity_points'] = _project_activity_points(STATE_DIR, path or str(ROOT))

        # Derive sessions — discover ALL tmux sessions, match to agents where possible
        sessions = []
        live_tmux: dict[str, dict] = {}
        claimed_tmux: set[str] = set()
        try:
            from tmux_capture import list_sessions as tmux_list
            for ts in tmux_list():
                live_tmux[ts.name] = {
                    'name': ts.name,
                    'windows': ts.windows,
                    'attached': ts.attached,
                }
        except Exception:
            pass

        # First: add Charon agents that have tmux sessions
        for a in agents:
            tmux_name = a.get('tmux_session', '')
            has_tmux = tmux_name in live_tmux
            if tmux_name:
                claimed_tmux.add(tmux_name)
            sessions.append({
                'id': f"session-{a['id']}",
                'agentId': a['id'],
                'agentName': a['name'],
                'sessionLabel': a['name'],
                'status': a['status'] if has_tmux else 'stopped',
                'project': a['project'].split('/')[-1] if a.get('project') else '',
                'location': 'local',
                'lastActivity': a.get('last_active', ''),
                'tmuxSession': tmux_name,
                'tmux_session': tmux_name,
                'hasTmux': has_tmux,
                'role': a.get('role', 'charon'),
                'source': 'charon',
            })

        # Boat-wrapped sessions (fast path for Hermes/Pi demo sessions)
        try:
            boat_dir = Path.home() / '.charon' / 'boats'
            if boat_dir.exists():
                for reg_file in sorted(boat_dir.glob('*.json')):
                    try:
                        reg = json.loads(reg_file.read_text())
                    except Exception:
                        continue
                    tmux_name = str(reg.get('session') or '').strip()
                    if not tmux_name:
                        continue
                    transport = str(reg.get('transport') or '').strip().lower()
                    reg_status = str(reg.get('status') or 'idle').strip() or 'idle'
                    has_tmux = tmux_name in live_tmux
                    sock_path = Path(str(reg.get('socket') or ''))
                    if transport in ('pty', 'charon'):
                        if reg_status not in ('running', 'starting') or not sock_path.exists():
                            continue
                    elif not has_tmux:
                        continue
                    if has_tmux and tmux_name in claimed_tmux:
                        continue
                    raw_name = str(reg.get('name') or tmux_name).strip() or tmux_name
                    command = str(reg.get('command') or '').strip()
                    base = command.split()[0] if command else raw_name
                    agent_target = Path(base).name.lower()
                    if agent_target.startswith('boat-'):
                        agent_target = raw_name.lower()
                    if transport == 'charon' or 'charon' in agent_target:
                        agent_name = 'Charon'
                        process_target = 'charon'
                    elif 'hermes' in agent_target:
                        agent_name = 'Hermes'
                        process_target = 'hermes'
                    elif agent_target == 'pi':
                        agent_name = 'Pi'
                        process_target = 'pi'
                    else:
                        agent_name = raw_name.split('-')[0].capitalize() or 'Agent'
                        process_target = agent_target or 'external'
                    if has_tmux:
                        claimed_tmux.add(tmux_name)
                    session_label = raw_name
                    if transport == 'charon' and raw_name and not raw_name.startswith('charon'):
                        session_label = f'charon-{raw_name}'
                    sessions.append({
                        'id': f'boat-{tmux_name}',
                        'agentId': f'boat-{tmux_name}',
                        'agentName': agent_name,
                        'sessionLabel': session_label,
                        'status': 'running' if has_tmux else reg_status,
                        'project': '',
                        'location': 'local',
                        'lastActivity': str(reg.get('created') or ''),
                        'tmuxSession': tmux_name,
                        'tmux_session': tmux_name,
                        'hasTmux': has_tmux,
                        'role': 'external',
                        'source': 'boat',
                        'processTarget': process_target,
                        'hasBoat': True,
                        'supportsCharonBoat': True,
                        'boatSessionId': raw_name,
                        'command': command[:80],
                        'transport': transport or ('pty' if sock_path.exists() else ''),
                        'socket': str(sock_path) if sock_path else '',
                    })
        except Exception:
            pass

        # Remote fleet agent sessions
        try:
            from fleet_registry import load_fleet as _fleet_load
            from fleet_sync import get_cached_fleet_status as _fleet_status
            _fleet = _fleet_load()
            _fstatus = _fleet_status()
            for _srv in _fleet.get('servers', []):
                _sid = _srv.get('id', _srv.get('host', ''))
                _sinfo = _fstatus.get(_sid, {})
                _ssessions = _sinfo.get('sessions', {})
                for _acfg in _srv.get('agents', []):
                    _aname = _acfg.get('name', '')
                    _sess_info = _ssessions.get(_aname, {})
                    _rstatus = _sess_info.get('status', 'offline') if _sinfo.get('online') else 'offline'
                    _remote_id = f"remote:{_sid}:{_aname}"
                    sessions.append({
                        'id': _remote_id,
                        'agentId': _remote_id,
                        'agentName': _aname,
                        'sessionLabel': f'{_aname} @ {_sid}',
                        'status': _rstatus,
                        'project': _acfg.get('project', '').split('/')[-1] if _acfg.get('project') else '',
                        'location': _srv.get('host', ''),
                        'lastActivity': '',
                        'tmuxSession': _sess_info.get('session_id', _aname),
                        'tmux_session': _sess_info.get('session_id', _aname),
                        'hasTmux': _rstatus in ('running', 'idle'),
                        'role': _acfg.get('type', 'remote'),
                        'source': 'fleet',
                        'transport': 'remote-boat',
                        'server_id': _sid,
                        'socket': '',
                    })
        except Exception:
            pass

        # Second: discover ALL running agent processes (pi, hermes, claude, etc.)
        # and match them to tmux sessions where possible
        detected_agents: list[dict] = []
        tmux_pane_map: dict[int, str] = {}  # pid → tmux session name
        try:
            import subprocess as _sp
            # Get tmux pane PIDs and commands to match processes to sessions
            result = _sp.run(
                ['tmux', 'list-panes', '-a', '-F', '#{session_name} #{pane_pid} #{pane_current_command}'],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                pane_pids: list[tuple[int, str]] = []
                for line in result.stdout.strip().splitlines():
                    parts = line.split(None, 2)
                    if len(parts) >= 2:
                        try:
                            pane_pids.append((int(parts[1]), parts[0]))
                            tmux_pane_map[int(parts[1])] = parts[0]
                        except ValueError:
                            pass
                # Map child and grandchild PIDs to their pane's tmux session
                for pane_pid, sess_name in pane_pids:
                    try:
                        cr = _sp.run(['pgrep', '-P', str(pane_pid)], capture_output=True, text=True, timeout=3)
                        if cr.returncode == 0:
                            for cl in cr.stdout.strip().splitlines():
                                try:
                                    cpid = int(cl.strip())
                                    tmux_pane_map[cpid] = sess_name
                                    # Grandchildren
                                    gr = _sp.run(['pgrep', '-P', str(cpid)], capture_output=True, text=True, timeout=3)
                                    if gr.returncode == 0:
                                        for gl in gr.stdout.strip().splitlines():
                                            try:
                                                tmux_pane_map[int(gl.strip())] = sess_name
                                            except ValueError:
                                                pass
                                except ValueError:
                                    pass
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            sys.path.insert(0, str(ROOT / 'apps' / 'tui'))
            from process_inspector import detect_agent_processes
            for proc in detect_agent_processes():
                # Skip if this PID belongs to a Charon agent tmux we already listed
                tmux_session = tmux_pane_map.get(proc.pid, '')
                if tmux_session in claimed_tmux:
                    continue
                agent_name = f"{proc.target}"
                if tmux_session:
                    agent_name = f"{proc.target} ({tmux_session})"
                    claimed_tmux.add(tmux_session)
                detected_agents.append({
                    'id': f"proc-{proc.pid}",
                    'agentId': f"proc-{proc.pid}",
                    'agentName': agent_name,
                    'status': 'running',
                    'project': '',
                    'location': 'local',
                    'lastActivity': '',
                    'tmuxSession': tmux_session,
                    'tmux_session': tmux_session,
                    'hasTmux': bool(tmux_session),
                    'role': 'external',
                    'source': 'detected',
                    'processTarget': proc.target,
                    'hasBoat': bool(getattr(proc, 'has_boat', False)),
                    'supportsCharonBoat': bool(getattr(proc, 'has_boat', False)),
                    'pid': proc.pid,
                    'command': proc.args[:80],
                })
        except Exception:
            pass

        sessions.extend(detected_agents)

        # Also add Charon agents as virtual sessions (viewable in grid as chat history)
        for a in agents:
            if a.get('role') != 'charon':
                continue
            aid = a['id']
            if any(s.get('agentId') == aid for s in sessions):
                continue  # already has a session
            sessions.append({
                'id': f"virtual-{aid}",
                'agentId': aid,
                'agentName': a['name'],
                'status': a['status'],
                'project': a['project'].split('/')[-1] if a.get('project') else '',
                'location': 'local',
                'lastActivity': a.get('last_active', ''),
                'tmuxSession': '',
                'tmux_session': '',
                'hasTmux': False,
                'role': 'charon',
                'source': 'virtual',
                'isVirtual': True,
            })

        # Third: add any remaining tmux sessions not claimed by agents or detected processes
        for tmux_name, tmux_info in live_tmux.items():
            if tmux_name in claimed_tmux:
                continue
            sessions.append({
                'id': f"tmux-{tmux_name}",
                'agentId': f"tmux-{tmux_name}",
                'agentName': f"tmux:{tmux_name}",
                'status': 'running',
                'project': '',
                'location': 'local',
                'lastActivity': '',
                'tmuxSession': tmux_name,
                'tmux_session': tmux_name,
                'hasTmux': True,
                'role': 'external',
                'source': 'tmux',
            })

        # Recent activity from run log
        activity = []
        run_log = STATE_DIR / 'run.log'
        if run_log.exists():
            try:
                lines = run_log.read_text().splitlines()[-15:]
                for line in lines:
                    try:
                        rec = json.loads(line)
                        evt = rec.get('event', '?')
                        tid = rec.get('task_id', '')
                        reason = rec.get('reason', '')
                        activity.append(f"{evt}: {tid} {reason}".strip())
                    except Exception:
                        pass
            except Exception:
                pass

        transfer_events = []
        try:
            from context_transfer import list_transfer_events
            transfer_events = list_transfer_events(STATE_DIR, limit=12)
        except Exception:
            transfer_events = []

        inter_agent_rooms = []
        try:
            from inter_agent_rooms import list_rooms, list_events
            for room in list_rooms(STATE_DIR, limit=40):
                rid = str(room.get('id') or '')
                if not rid:
                    continue
                item = dict(room)
                item['events'] = list_events(STATE_DIR, rid, limit=80)
                inter_agent_rooms.append(item)
        except Exception:
            inter_agent_rooms = []

        # Map Libris and software-dev operations into the shared F4 room list so
        # F4 can render them with a graph-first layout later.
        project_root = Path(str(onboarding.get('project') or str(ROOT)).strip() or str(ROOT))
        try:
            from libris_runtime import rebuild_project_index, get_libris_swarm_state
            idx = rebuild_project_index(STATE_DIR, project_root)
            for op in idx.get('operations') or []:
                op_id = str(op.get('operation_id') or '').strip()
                if not op_id:
                    continue
                swarm = get_libris_swarm_state(STATE_DIR, project_root, op_id)
                if not swarm:
                    continue
                inter_agent_rooms.append({
                    'id': f'libris-{op_id}',
                    'kind': 'libris',
                    'title': str(op.get('prompt') or op_id)[:120],
                    'project': str(project_root),
                    'status': str(swarm.get('status') or op.get('status') or 'active'),
                    'created_at': str(op.get('created_at') or ''),
                    'updated_at': str(op.get('updated_at') or ''),
                    'last_activity': str(op.get('updated_at') or op.get('created_at') or ''),
                    'participants': [
                        {
                            'id': str(n.get('agent_id') or ''),
                            'name': str(n.get('name') or ''),
                            'role': str(n.get('role') or ''),
                            'status': str(n.get('status') or ''),
                        }
                        for n in (swarm.get('nodes') or [])
                    ],
                    'summary': str(swarm.get('prompt') or '')[:200],
                    'operation_id': op_id,
                    'nodes': swarm.get('nodes') or [],
                    'edges': swarm.get('edges') or [],
                    'topics': swarm.get('topics') or [],
                    'team_grid_nodes': swarm.get('team_grid_nodes') or [],
                    'non_shade_members': swarm.get('non_shade_members') or [],
                    'views': swarm.get('views') or {},
                    'counts': swarm.get('counts') or {},
                    'budget_status': swarm.get('budget_status') or {},
                    'promising_sources': swarm.get('promising_sources') or [],
                    'executive_summary_markdown': swarm.get('executive_summary_markdown') or '',
                    'delivery_bundle': swarm.get('delivery_bundle') or {},
                    'final_selection_markdown': swarm.get('final_selection_markdown') or '',
                    'events': swarm.get('events_tail') or [],
                })
        except Exception:
            pass

        try:
            inter_agent_rooms.extend(_collect_devop_rooms(STATE_DIR, project_root))
        except Exception:
            pass

        session_lookup = {}
        for s in sessions:
            sid = str(s.get('tmuxSession') or s.get('tmux_session') or s.get('id') or '').strip()
            if sid:
                session_lookup[sid] = s
            raw_boat = str(s.get('boatSessionId') or '').strip()
            if raw_boat:
                session_lookup[raw_boat if raw_boat.startswith('boat-') else f'boat-{raw_boat}'] = s
        for room in inter_agent_rooms:
            participant_sessions = room.get('participant_sessions') or []
            participants = room.get('participants') or []
            if not participant_sessions and participants:
                participant_sessions = [p.get('session') for p in participants if p.get('session')]
                room['participant_sessions'] = participant_sessions
            room['session_details'] = [session_lookup[s] for s in participant_sessions if s in session_lookup]

        automations = []
        try:
            from automation_runtime import list_automations, get_automation_state
            automations = [get_automation_state(STATE_DIR, str(a.get('automation_id') or '')) for a in list_automations(STATE_DIR)]
        except Exception:
            automations = []

        payload = {
            'onboarding': {
                'complete': complete,
                'provider': provider,
                'model': model,
                'step': effective_onboarding.get('step', 'provider-mode'),
                'project': str(effective_onboarding.get('project') or '').strip(),
            },
            'agents': agents,
            'projects': projects,
            'sessions': sessions,
            'activity': activity,
            'transfer_events': transfer_events,
            'inter_agent_rooms': inter_agent_rooms,
            'automations': automations,
            'dashboard': {
                'agents_row': {'items': agents},
                'projects_row': {'items': projects},
                'automations_row': {'items': automations},
            },
            'chat_history': self.chat_history[-200:],
            'engine_ready': self.engine is not None,
            'message_count': len(self.engine.messages) if self.engine else 0,
            'agent_mode': self.agent_mode,
            'session_info': self._get_session_info(),
            'batch_progress': self._get_batch_progress(),
            'orchestration_parse': dict(self._last_orchestration_parse or {}),
        }

        # Include recent consolidation traces for dashboard
        try:
            from consolidation import list_traces
            payload['consolidation_traces'] = list_traces(STATE_DIR, limit=5)
        except Exception:
            payload['consolidation_traces'] = []

        return payload

    def _project_root_for_rooms(self) -> Path:
        onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
        return Path(str(onboarding.get('project') or str(ROOT)).strip() or str(ROOT))

    def _load_libris_room(self, room_id: str) -> dict | None:
        rid = str(room_id or '').strip()
        if not rid.startswith('libris-'):
            return None
        op_id = rid[len('libris-'):].strip()
        if not op_id:
            return None
        try:
            from libris_runtime import get_libris_swarm_state
            project_root = self._project_root_for_rooms()
            swarm = get_libris_swarm_state(STATE_DIR, project_root, op_id)
            if not swarm:
                return None
            return {
                'id': rid,
                'kind': 'libris',
                'operation_id': op_id,
                'title': str(swarm.get('prompt') or op_id)[:120],
                'status': str(swarm.get('status') or 'active'),
                'participants': [
                    {
                        'id': str(n.get('agent_id') or ''),
                        'name': str(n.get('name') or ''),
                        'role': str(n.get('role') or ''),
                        'topic_slug': str(n.get('topic_slug') or ''),
                    }
                    for n in (swarm.get('nodes') or [])
                    if str(n.get('agent_id') or '').strip()
                ],
                'nodes': swarm.get('nodes') or [],
                'topics': swarm.get('topics') or [],
            }
        except Exception:
            return None

    def _resolve_libris_targets(self, room: dict, target: str) -> tuple[list[dict], str]:
        nodes = [n for n in (room.get('nodes') or []) if isinstance(n, dict)]
        topics = [t for t in (room.get('topics') or []) if isinstance(t, dict)]
        token = str(target or 'whole').strip()
        tl = token.lower()
        if tl in ('', 'whole', 'room', 'all', '*'):
            return [n for n in nodes if str(n.get('agent_id') or '').strip()], 'whole room'
        if tl == 'coordinator':
            out = [n for n in nodes if str(n.get('role') or '').lower() == 'coordinator']
            return out, 'coordinator'
        if tl.startswith('node:'):
            node_id = token.split(':', 1)[1].strip()
            out = [n for n in nodes if str(n.get('agent_id') or '') == node_id]
            return out, f'node:{node_id}'
        if tl.startswith('agent:'):
            node_id = token.split(':', 1)[1].strip()
            out = [n for n in nodes if str(n.get('agent_id') or '') == node_id]
            return out, f'agent:{node_id}'
        if tl.startswith('topic:'):
            slug = token.split(':', 1)[1].strip()
            out: list[dict] = []
            for topic in topics:
                if str(topic.get('topic_slug') or '') != slug:
                    continue
                for key in ('researcher', 'judge'):
                    node = topic.get(key) or {}
                    if str(node.get('agent_id') or '').strip():
                        out.append(node)
                break
            return out, f'topic:{slug}'
        if tl.startswith('researcher:') or tl.startswith('judge:'):
            role, slug = tl.split(':', 1)
            out = [n for n in nodes if str(n.get('role') or '').lower() == role and str(n.get('topic_slug') or '') == slug]
            return out, f'{role}:{slug}'
        if tl.startswith('shade:'):
            shade_id = token.split(':', 1)[1].strip()
            out = [n for n in nodes if str(n.get('role') or '').lower() == 'shade' and str(n.get('agent_id') or '') == shade_id]
            return out, f'shade:{shade_id}'
        out = [
            n for n in nodes
            if tl in str(n.get('agent_id') or '').lower()
            or tl in str(n.get('name') or '').lower()
            or tl == str(n.get('role') or '').lower()
        ]
        return out, token

    def _dispatch_libris_room_intervention(self, room_id: str, *, target: str, when: str, message: str, request_id: str | None, mode: str = 'inject') -> bool:
        room = self._load_libris_room(room_id)
        if not room:
            emit({'type': 'error', 'error': f'Unknown room: {room_id}', 'request_id': request_id})
            return True
        targets, target_label = self._resolve_libris_targets(room, target)
        if not targets:
            emit({'type': 'error', 'error': f'No Libris targets matched: {target}', 'request_id': request_id})
            return True
        try:
            from session_registry import send_steer
            from libris_runtime import append_operation_event
            project_root = self._project_root_for_rooms()
            sent: list[str] = []
            for node in targets:
                agent_id = str(node.get('agent_id') or '').strip()
                if not agent_id:
                    continue
                if send_steer(STATE_DIR, agent_id, message):
                    sent.append(agent_id)
            append_operation_event(
                STATE_DIR,
                project_root,
                str(room.get('operation_id') or ''),
                'operator_intervention',
                {
                    'mode': mode,
                    'room_id': room_id,
                    'target': target_label,
                    'requested_target': str(target or ''),
                    'when': str(when or 'next'),
                    'summary': str(message or '')[:240],
                    'message': str(message or ''),
                    'target_agent_ids': sent,
                },
            )
            emit({
                'type': 'status',
                'message': f'{"Sent" if mode == "say" else "Queued"} Libris intervention for {room_id} target={target_label} agents={len(sent)}: {message[:120]}',
                'request_id': request_id,
            })
            self.handle_refresh(request_id)
            return True
        except Exception as e:
            emit({'type': 'error', 'error': f'Libris intervention failed: {e}', 'request_id': request_id})
            return True

    def handle_refresh(self, request_id: str | None):
        payload = self._get_refresh_payload()
        payload['session_id'] = self._active_agent_id or ''
        payload['visible_thoughts'] = self.visible_thoughts
        payload['thoughts_supported'] = self._thoughts_supported()

        # Check for incoming steering messages from other Charon instances
        if self._active_agent_id:
            try:
                from session_registry import read_steers
                steers = read_steers(STATE_DIR, self._active_agent_id)
                for steer in steers:
                    msg = steer.get('message', '')
                    if msg:
                        emit({
                            'type': 'status',
                            'message': f'📡 Message from another Charon: {msg[:80]}',
                            'request_id': request_id,
                        })
                        # Submit as a regular chat message so the agent responds
                        import threading
                        threading.Thread(
                            target=self.handle_chat,
                            args=(f'[Steering from another Charon session] {msg}', request_id),
                            daemon=True,
                        ).start()
            except Exception:
                pass

        # Heartbeat + include live Charon sessions
        try:
            from session_registry import heartbeat, list_live_sessions
            if self._active_agent_id:
                heartbeat(STATE_DIR, self._active_agent_id)
            live = list_live_sessions(STATE_DIR)
            # Add live sessions as agents (if not already present)
            for ls in live:
                sid = ls.get('session_id', '')
                if sid == self._active_agent_id:
                    continue  # skip self
                if not ls.get('alive', False):
                    continue
                # Add as a session entry
                payload.setdefault('sessions', []).append({
                    'id': f'live-{sid}',
                    'agentId': f'live-{sid}',
                    'agentName': f'charon ({sid.split("-")[-1][:6]})',
                    'status': 'running',
                    'project': '',
                    'location': 'local',
                    'lastActivity': '',
                    'tmuxSession': '',
                    'tmux_session': '',
                    'hasTmux': False,
                    'role': 'charon',
                    'source': 'live',
                    'isLive': True,
                    'liveSessionId': sid,
                })
        except Exception:
            pass

        emit({
            'type': 'refresh',
            'request_id': request_id,
            'payload': payload,
        })

    def _current_provider_name(self) -> str:
        try:
            from provider_bridge import resolve_provider_config
            cfg = resolve_provider_config(STATE_DIR)
            return str(cfg.get('provider_name') or cfg.get('provider_raw') or '').strip().lower()
        except Exception:
            onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
            return str(onboarding.get('provider') or '').strip().lower()

    def _thoughts_supported(self) -> bool:
        name = self._current_provider_name()
        # Anthropic has native thinking. Local/OpenAI-compatible providers may
        # also surface thoughts via reasoning fields or inline <think> blocks.
        return name in {'anthropic', 'openai', 'local'}

    def _libris_project_root(self) -> str:
        onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
        configured_project = str(onboarding.get('project') or '').strip()
        return configured_project or str(ROOT)

    def _devop_project_root(self) -> str:
        onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
        configured_project = str(onboarding.get('project') or '').strip()
        return configured_project or str(ROOT)

    def _libris_goal_options(self, prompt: str) -> list[str]:
        lower = (prompt or '').lower()
        if 'computer vision' in lower or 'vision' in lower:
            return [
                'Identify the most practically important new techniques worth implementing or prototyping.',
                'Focus on the highest-novelty research directions from the last few months, even if speculative.',
                'Prioritize methods with strong evidence, benchmarks, code availability, and likely near-term impact.',
            ]
        if 'reinforcement learning' in lower or 'rl' in lower:
            return [
                'Prioritize techniques most likely to improve our current RL work in practice.',
                'Focus on the most novel and strategically important RL directions from recent months.',
                'Prefer methods with strong empirical evidence, code, and realistic implementation paths.',
            ]
        return [
            'Prioritize practical, high-impact techniques we could plausibly act on.',
            'Focus on novelty and strategic importance, even if implementation is less immediate.',
            'Prefer evidence-backed methods with code, benchmarks, and clear adoption signals.',
        ]

    def _libris_extract_stop(self, text: str) -> str:
        t = (text or '').strip()
        m = re.search(r'(stop after .+|run for .+|for \d+ (?:hours?|days?|weeks?)|until i stop you|until stopped|cap(?: it)? at .+ tokens?|under .+ tokens?)', t, re.I)
        return m.group(1).strip() if m else ''

    def _libris_parse_budget(self, stop_condition: str) -> dict:
        t = (stop_condition or '').lower().strip()
        out: dict = {}
        if not t:
            return out
        m = re.search(r'(\d+)\s*(hour|hours|day|days|week|weeks)', t)
        if m:
            n = int(m.group(1))
            unit = m.group(2)
            hours = n
            if 'day' in unit:
                hours = n * 24
            elif 'week' in unit:
                hours = n * 24 * 7
            out['max_wall_hours'] = hours
        m = re.search(r'(\d+(?:[\.,]\d+)?)\s*(m|million)?\s*tokens?', t)
        if m:
            num = float(m.group(1).replace(',', ''))
            if m.group(2) == 'm':
                num *= 1_000_000
            out['max_total_tokens'] = int(num)
        m = re.search(r'\$\s*(\d+(?:\.\d+)?)', t)
        if m:
            out['max_total_cost_usd'] = float(m.group(1))
        return out

    def _libris_has_clear_goal(self, text: str) -> bool:
        t = (text or '').lower()
        patterns = [
            r'priorit', r'focus on', r'looking for', r'goal is', r'what we care about',
            r'practical', r'novel', r'implementation', r'actionable', r'benchmark',
        ]
        return any(re.search(p, t) for p in patterns)

    def _emit_libris_intake(self, request_id: str | None) -> None:
        pending = self._pending_libris_intake or {}
        options = pending.get('goal_options') or []
        prompt = pending.get('prompt') or ''
        stop = pending.get('stop_condition') or '(none set)'
        lines = [
            'Libris intake: before starting, I want to make sure we have a clear research standard.',
            '',
            f'Research prompt: {prompt}',
            f'Stop condition: {stop}',
            '',
            'Suggested research goals:',
        ]
        for i, opt in enumerate(options, 1):
            lines.append(f'{i}. {opt}')
        lines.extend([
            '',
            'Reply with one of:',
            '/libris use 1      choose a suggested goal',
            '/libris use 2',
            '/libris use 3',
            '/libris custom <goal>',
            '/libris stop <condition>',
            '/libris go         start with the current selections',
        ])
        emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})

    def _start_libris_from_pending(self, request_id: str | None) -> None:
        pending = self._pending_libris_intake or {}
        prompt = str(pending.get('prompt') or '').strip()
        if not prompt:
            emit({'type': 'error', 'error': 'No pending Libris intake.', 'request_id': request_id})
            return
        selected_goal = str(pending.get('selected_goal') or '').strip()
        stop_condition = str(pending.get('stop_condition') or '').strip()
        budget = self._libris_parse_budget(stop_condition)
        full_prompt = prompt
        if selected_goal:
            full_prompt += f'\n\nResearch goal standard: {selected_goal}'
        if stop_condition:
            full_prompt += f'\n\nStop condition: {stop_condition}'
        try:
            from libris_agents import start_autonomous_libris_research
            res = start_autonomous_libris_research(
                STATE_DIR,
                Path(self._libris_project_root()),
                prompt=full_prompt,
                parent_agent_id=self._active_agent_id or '',
                budget=budget,
            )
            op = res.get('operation') or {}
            coord = res.get('coordinator') or {}
            self._pending_libris_intake = None
            emit({
                'type': 'status',
                'message': (
                    f'Libris research started.\n'
                    f'Operation: {op.get("operation_id")}\n'
                    f'Coordinator: {coord.get("id")} ({coord.get("name")})\n'
                    f'Budget: {budget or "(none set)"}\n'
                    f'Use /libris status {op.get("operation_id")} to inspect swarm state.'
                ),
                'request_id': request_id,
            })
            emit({'type': 'refresh', 'payload': {'session_info': self._get_session_info()}, 'request_id': request_id})
            emit({'type': 'libris_started', 'operation_id': op.get('operation_id'), 'request_id': request_id})
        except Exception as e:
            emit({'type': 'error', 'error': f'Failed to start Libris research: {e}', 'request_id': request_id})

    def _command_catalog(self) -> list[dict]:
        """Return available commands with descriptions."""
        return [
            {'cmd': '/help', 'desc': 'Show available commands'},
            {'cmd': '/setup', 'desc': 'Show setup commands'},
            {'cmd': '/setup status', 'desc': 'Show current configuration'},
            {'cmd': '/setup reset', 'desc': 'Reset all configuration'},
            {'cmd': '/setup provider lmstudio', 'desc': 'Use local LM Studio'},
            {'cmd': '/setup provider claude-code', 'desc': 'Use Anthropic Claude (OAuth)'},
            {'cmd': '/setup provider codex', 'desc': 'Use OpenAI Codex (OAuth)'},
            {'cmd': '/setup provider api', 'desc': 'Use custom API endpoint'},
            {'cmd': '/setup model <name>', 'desc': 'Set model name'},
            {'cmd': '/setup api-key <key>', 'desc': 'Set API key directly'},
            {'cmd': '/setup project <path>', 'desc': 'Set project directory'},
            {'cmd': '/setup complete', 'desc': 'Finish setup'},
            {'cmd': '/setup no-provider', 'desc': 'Skip LLM setup (heuristic only)'},
            {'cmd': '/model', 'desc': 'Show current model'},
            {'cmd': '/reset', 'desc': 'Clear conversation'},
            {'cmd': '/dashboard', 'desc': 'Switch to dashboard (F2)'},
            {'cmd': '/sessions', 'desc': 'Switch to sessions (F3)'},
            {'cmd': '/chat', 'desc': 'Switch to chat (F1)'},
            {'cmd': '/hermes', 'desc': 'Launch a wrapped Hermes session in the background'},
            {'cmd': '/pi', 'desc': 'Launch a wrapped pi session in the background'},
            {'cmd': '/conversation hermes teacher student <topic>', 'desc': 'Start a teacher/student Hermes conversation room'},
            {'cmd': '/conversation hermes strategist critic <topic>', 'desc': 'Start a strategist/critic Hermes conversation room'},
            {'cmd': '/conversation hermes planner critic <topic>', 'desc': 'Start a planner/critic Hermes conversation room'},
            {'cmd': '/conversation hermes architect reviewer <topic>', 'desc': 'Start an architect/reviewer Hermes conversation room'},
            {'cmd': '/conversation hermes optimist skeptic <topic>', 'desc': 'Start an optimist/skeptic Hermes conversation room'},
            {'cmd': '/conversation hermes dialogue <topic>', 'desc': 'Start a peer philosophy dialogue between two Hermes agents'},
            {'cmd': '/conversation hermes 2 <topic>', 'desc': 'Start a 2-agent Hermes conversation room'},
            {'cmd': '/team hermes <count> <topic>', 'desc': 'Create a Hermes discussion room/team'},
            {'cmd': '/devteam hermes <count> <goal>', 'desc': 'Create a Hermes developer team room'},
            {'cmd': '/pause-room <room-id>', 'desc': 'Pause a conversation room runner'},
            {'cmd': '/resume-room <room-id>', 'desc': 'Resume a paused conversation room runner'},
            {'cmd': '/say-room <room-id> <message>', 'desc': 'Say something to the whole room so both sides can react'},
            {'cmd': '/inject-room <room-id> [--target whole|teacher|student|<participant>|coordinator|topic:<slug>|node:<agent-id>|researcher:<slug>|judge:<slug>|shade:<agent-id>] [--when now|next] <message>', 'desc': 'Inject steering or a message into a room'},
            {'cmd': '/libris <prompt>', 'desc': 'Start a Libris research intake for a broad research prompt'},
            {'cmd': '/libris status <operation_id>', 'desc': 'Inspect Libris swarm state for an operation'},
            {'cmd': '/devop <prompt>', 'desc': 'Start an autonomous software-development operation for a broad build prompt'},
            {'cmd': '/devop status <operation_id>', 'desc': 'Inspect software-dev operation status and workstreams'},
            {'cmd': '/devop stop <operation_id>', 'desc': 'Request stop for a software-dev operation'},
            {'cmd': '/monitor every hour <url>', 'desc': 'Create a recurring website health monitor'},
            {'cmd': '/automate every <n> <unit> check <url>', 'desc': 'Create a recurring automation for HTTP checking'},
            {'cmd': '/automate list', 'desc': 'List all automations'},
            {'cmd': '/automate list cron', 'desc': 'List cron-scheduled automations'},
            {'cmd': '/automate list continuous', 'desc': 'List always-on continuous automations'},
            {'cmd': '/automate list scheduled', 'desc': 'List interval/cron scheduled automations'},
            {'cmd': '/automate status <automation_id>', 'desc': 'Inspect an automation and recent runs'},
            {'cmd': '/automate pause <automation_id>', 'desc': 'Pause a recurring automation'},
            {'cmd': '/automate resume <automation_id>', 'desc': 'Resume a paused recurring automation'},
            {'cmd': '/automate stop <automation_id>', 'desc': 'Stop a recurring automation'},
            {'cmd': '/automate cron "0 9 * * 1-5" check <url>', 'desc': 'Create a cron-scheduled automation'},
            {'cmd': '/automate continuous every <n> seconds check <url>', 'desc': 'Create an always-on loop automation'},
            {'cmd': '/monitor browser every hour <url> expect "text"', 'desc': 'Create a browser-rendered functional monitor'},
            {'cmd': '/automate browser every <n> <unit> check <url> expect "text"', 'desc': 'Create a browser-based rendered-page monitor'},
            {'cmd': '/automate browser-workflow every <n> <unit> steps <json>', 'desc': 'Create a multi-step browser workflow automation'},
            {'cmd': '/automate browser-workflow every <n> <unit> from <file>', 'desc': 'Create a multi-step browser workflow automation from a JSON file'},
            {'cmd': '/automate webhook <automation_id> <url>', 'desc': 'Set a webhook for automation failure/recovery alerts'},
        ]

    def _get_suggestions(self, prefix: str) -> list[dict]:
        """Get matching commands for a prefix."""
        prefix = prefix.strip().lower()
        catalog = self._command_catalog()
        if not prefix or prefix == '/':
            return catalog

        starts = [c for c in catalog if c['cmd'].lower().startswith(prefix)]
        if starts:
            return starts[:30]

        token_matches: list[dict] = []
        needle = prefix.lstrip('/')
        for item in catalog:
            cmd = item['cmd'].lower()
            parts = [p for p in cmd.replace('/', ' ').replace('<', ' ').replace('>', ' ').split() if p]
            if any(part.startswith(needle) for part in parts):
                token_matches.append(item)
        if token_matches:
            return token_matches[:10]

        return []

    def _ensure_session_id(self) -> str:
        if not self._active_agent_id:
            import time, hashlib
            raw = f'{time.time()}-{os.getpid()}'
            short = hashlib.md5(raw.encode()).hexdigest()[:6]
            self._active_agent_id = f'session-{short}-{int(time.time())}'
        return self._active_agent_id

    def _session_provider_state(self) -> dict:
        try:
            session_id = self._active_agent_id or None
            return resolve_provider_config(STATE_DIR, session_id=session_id)
        except Exception:
            onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
            return {
                'provider_raw': str(onboarding.get('provider') or '').strip(),
                'model_id': str(onboarding.get('model') or onboarding.get('provider_model') or '').strip(),
                'ready': bool(onboarding.get('complete')),
            }

    def _current_provider_name(self) -> str:
        if self.engine is not None:
            return str(getattr(self.engine, 'provider_name', '') or '').strip()
        state = self._session_provider_state()
        return str(state.get('provider_raw') or state.get('provider_name') or '').strip()

    def _has_transferable_context(self) -> bool:
        try:
            from context_transfer import session_has_transferable_context
            return bool(self.engine and session_has_transferable_context(self.engine.messages))
        except Exception:
            return bool(self.engine and len(self.engine.messages) >= 4)

    def _prompt_provider_switch(self, target_provider: str, request_id: str | None, source: str):
        current_provider = self._current_provider_name() or 'current provider'
        self._pending_provider_switch = {
            'target_provider': target_provider,
            'source_provider': current_provider,
            'source': source,
        }
        emit({
            'type': 'status',
            'message': (
                f'Switching from {current_provider} to {target_provider}. '
                'Choose whether to continue this session or start fresh.'
            ),
            'request_id': request_id,
        })
        emit({
            'type': 'suggestions',
            'title': 'Provider Switch',
            'items': [
                {
                    'cmd': '/1',
                    'label': '1',
                    'desc': f'Continue this session with {target_provider} using context transfer',
                },
                {
                    'cmd': '/2',
                    'label': '2',
                    'desc': f'Start a new {target_provider} session',
                },
            ],
            'request_id': request_id,
        })

    def _switch_provider_with_transfer(self, target_provider: str, request_id: str | None):
        bundle = None
        if self.engine and self._active_agent_id:
            try:
                from context_transfer import create_transfer_bundle, record_pending_transfer, record_transfer_event
                bundle = create_transfer_bundle(
                    state_dir=STATE_DIR,
                    session_id=self._active_agent_id,
                    agent_id=(getattr(self, '_bound_agent_id', None) or self._active_agent_id),
                    project_root=self.engine.project_root,
                    source_provider=self._current_provider_name() or 'unknown',
                    target_provider=target_provider,
                    messages=self.engine.messages,
                )
                record_pending_transfer(STATE_DIR, bundle)
                record_transfer_event(STATE_DIR, {
                    'ts': bundle.get('created_at', ''),
                    'type': 'provider_switch_continue',
                    'bundle_id': bundle.get('id', ''),
                    'source_provider': self._current_provider_name() or 'unknown',
                    'target_provider': target_provider,
                    'session_id': self._active_agent_id,
                })
                emit({
                    'type': 'status',
                    'message': f'Preparing context transfer to {target_provider} ({bundle.get("id", "")})...',
                    'request_id': request_id,
                })
                emit({
                    'type': 'status',
                    'message': f'Context transfer ready. Switching provider to {target_provider}...',
                    'request_id': request_id,
                })
            except Exception as e:
                emit({
                    'type': 'status',
                    'message': f'Context transfer prep failed ({e}). Falling back to fresh switch.',
                    'request_id': request_id,
                })
        self._run_setup_command(f'provider {target_provider}', request_id, skip_prompt=True)

    def _switch_provider_fresh(self, target_provider: str, request_id: str | None):
        emit({
            'type': 'status',
            'message': f'Starting a fresh {target_provider} session...',
            'request_id': request_id,
        })
        try:
            from context_transfer import clear_pending_transfer, record_transfer_event
            clear_pending_transfer(STATE_DIR)
            record_transfer_event(STATE_DIR, {
                'ts': __import__('time').strftime('%Y-%m-%dT%H:%M:%SZ', __import__('time').gmtime()),
                'type': 'provider_switch_fresh',
                'source_provider': self._current_provider_name() or 'unknown',
                'target_provider': target_provider,
                'session_id': self._active_agent_id or '',
            })
        except Exception:
            pass
        self._run_setup_command(f'provider {target_provider}', request_id, skip_prompt=True)

    def handle_command(self, command: str, request_id: str | None):
        """Handle /setup and other slash commands."""
        command = command.strip()
        if not command:
            return

        try:
            if self._pending_provider_switch and command in ('/1', '/2'):
                pending = self._pending_provider_switch
                self._pending_provider_switch = None
                if command == '/1':
                    self._switch_provider_with_transfer(str(pending.get('target_provider') or ''), request_id)
                else:
                    self._switch_provider_fresh(str(pending.get('target_provider') or ''), request_id)
                return

            # Show suggestions for /help, /setup alone, or unknown commands
            if command in ('/help', '/setup', '/?'):
                suggestions = self._get_suggestions('/setup' if command == '/setup' else '/')
                emit({
                    'type': 'suggestions',
                    'title': 'Setup Commands' if command == '/setup' else 'Available Commands',
                    'items': suggestions,
                    'request_id': request_id,
                })
                return

            if command.startswith('/setup '):
                rest = command[7:].strip()
                self._run_setup_command(rest, request_id)
                return
            if command == '/libris' or command.startswith('/libris '):
                rest = command[8:].strip() if command.startswith('/libris ') else ''
                if not rest:
                    self._pending_libris_intake = {
                        'prompt': '',
                        'goal_options': [],
                        'selected_goal': '',
                        'stop_condition': '',
                    }
                    emit({'type': 'status', 'message': 'Usage: /libris <broad research prompt>', 'request_id': request_id})
                    return
                if rest.startswith('status '):
                    op_id = rest[7:].strip()
                    try:
                        from libris_runtime import get_libris_swarm_state
                        swarm = get_libris_swarm_state(STATE_DIR, Path(self._libris_project_root()), op_id)
                        if not swarm:
                            emit({'type': 'error', 'error': f'No Libris operation found: {op_id}', 'request_id': request_id})
                            return
                        lines = [
                            f'Operation: {swarm.get("operation_id")}',
                            f'Status: {swarm.get("status")}',
                            f'Topics: {len(swarm.get("topics") or [])}',
                        ]
                        coord = swarm.get('coordinator') or {}
                        if coord:
                            lines.append(f'Coordinator: {coord.get("name")} [{coord.get("status")}]')
                        for topic in swarm.get('topics') or []:
                            lines.append(f'- {topic.get("title")} [{topic.get("status")}/{topic.get("phase")}]')
                        emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                    except Exception as e:
                        emit({'type': 'error', 'error': f'Libris status failed: {e}', 'request_id': request_id})
                    return
                if rest.startswith('use '):
                    choice = rest[4:].strip()
                    pending = self._pending_libris_intake or {}
                    options = list(pending.get('goal_options') or [])
                    if choice.isdigit() and 1 <= int(choice) <= len(options):
                        pending['selected_goal'] = options[int(choice) - 1]
                        self._pending_libris_intake = pending
                        emit({'type': 'status', 'message': f'Selected Libris goal: {pending["selected_goal"]}', 'request_id': request_id})
                    else:
                        emit({'type': 'error', 'error': f'Invalid Libris goal option: {choice}', 'request_id': request_id})
                    return
                if rest.startswith('custom '):
                    goal = rest[7:].strip()
                    if not goal:
                        emit({'type': 'error', 'error': 'Custom Libris goal cannot be empty.', 'request_id': request_id})
                        return
                    pending = self._pending_libris_intake or {}
                    pending['selected_goal'] = goal
                    self._pending_libris_intake = pending
                    emit({'type': 'status', 'message': f'Set custom Libris goal: {goal}', 'request_id': request_id})
                    return
                if rest.startswith('stop '):
                    cond = rest[5:].strip()
                    pending = self._pending_libris_intake or {}
                    pending['stop_condition'] = cond
                    self._pending_libris_intake = pending
                    emit({'type': 'status', 'message': f'Set Libris stop condition: {cond}', 'request_id': request_id})
                    return
                if rest == 'go':
                    self._start_libris_from_pending(request_id)
                    return

                pending = {
                    'prompt': rest,
                    'goal_options': self._libris_goal_options(rest),
                    'selected_goal': '',
                    'stop_condition': self._libris_extract_stop(rest),
                }
                self._pending_libris_intake = pending
                if self._libris_has_clear_goal(rest):
                    pending['selected_goal'] = rest
                self._emit_libris_intake(request_id)
                return
            if command == '/devop' or command.startswith('/devop '):
                rest = command[7:].strip() if command.startswith('/devop ') else ''
                if not rest:
                    emit({'type': 'status', 'message': 'Usage: /devop <broad software build prompt>', 'request_id': request_id})
                    return
                if rest.startswith('status '):
                    op_id = rest[7:].strip()
                    try:
                        from devop_projection import summarize_operation
                        from devop_runtime import get_operation_state
                        op = get_operation_state(STATE_DIR, op_id)
                        if not op:
                            emit({'type': 'error', 'error': f'No software-dev operation found: {op_id}', 'request_id': request_id})
                            return
                        summary = summarize_operation(STATE_DIR, op_id)
                        lines = [
                            f'Operation: {op.get("operation_id")}',
                            f'Status: {op.get("status")}',
                            f'Workstreams: {summary.get("workstream_count", 0)}',
                            f'Checkpoints: {summary.get("checkpoint_count", 0)}',
                            f'Reviews: {summary.get("review_count", 0)}',
                        ]
                        for ws in op.get('workstreams') or []:
                            latest_review = ws.get('latest_review') or {}
                            latest_checkpoint = ws.get('latest_checkpoint') or {}
                            lines.append(
                                f'- {ws.get("title") or ws.get("slug")} '
                                f'[{ws.get("status")}] '
                                f'cp={latest_checkpoint.get("checkpoint_id") or "-"} '
                                f'review={latest_review.get("decision") or "-"}'
                            )
                        emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                    except Exception as e:
                        emit({'type': 'error', 'error': f'Software-dev status failed: {e}', 'request_id': request_id})
                    return
                if rest.startswith('stop '):
                    op_id = rest[5:].strip()
                    try:
                        from devop_runtime import operation_dir, append_operation_event, set_operation_status
                        op_path = operation_dir(STATE_DIR, op_id) / 'operation.json'
                        if not op_path.exists():
                            emit({'type': 'error', 'error': f'No software-dev operation found: {op_id}', 'request_id': request_id})
                            return
                        op_doc = _load_json(op_path, {})
                        op_doc['stop_requested'] = True
                        op_doc['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                        op_path.write_text(json.dumps(op_doc, indent=2, ensure_ascii=False))
                        append_operation_event(STATE_DIR, op_id, 'stop_requested', summary='User requested stop.')
                        set_operation_status(STATE_DIR, op_id, 'stopping', 'User requested stop.')
                        emit({'type': 'status', 'message': f'Requested stop for software-dev operation {op_id}', 'request_id': request_id})
                        emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                    except Exception as e:
                        emit({'type': 'error', 'error': f'Software-dev stop failed: {e}', 'request_id': request_id})
                    return
                try:
                    from devop_agents import start_autonomous_software_operation
                    res = start_autonomous_software_operation(
                        STATE_DIR,
                        Path(self._devop_project_root()),
                        prompt=rest,
                        parent_agent_id=self._active_agent_id or '',
                    )
                    op = res.get('operation') or {}
                    coord = res.get('coordinator') or {}
                    emit({
                        'type': 'status',
                        'message': (
                            f'Started software-dev operation.\n'
                            f'Operation: {op.get("operation_id")}\n'
                            f'Coordinator: {coord.get("name") or coord.get("id") or "(starting)"}\n'
                            f'Use /devop status {op.get("operation_id")} to inspect progress.'
                        ),
                        'request_id': request_id,
                    })
                    emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': f'Failed to start software-dev operation: {e}', 'request_id': request_id})
                return
            if command == '/monitor' or command.startswith('/monitor '):
                rest = command[8:].strip() if command.startswith('/monitor ') else ''
                if not rest:
                    emit({'type': 'status', 'message': 'Usage: /monitor every hour <url>', 'request_id': request_id})
                    return
                browser_mode = rest.lower().startswith('browser ')
                if browser_mode:
                    rest = rest[8:].strip()
                interval = _parse_interval_phrase(rest)
                url_match = re.search(r'(https?://\S+)', rest)
                if interval <= 0 or not url_match:
                    emit({'type': 'error', 'error': 'Usage: /monitor every hour <url>', 'request_id': request_id})
                    return
                url = url_match.group(1).rstrip('.,)')
                expect_match = re.search(r'\b(?:expect|contains?)\s+"([^"]+)"', rest, re.I)
                expected_text = expect_match.group(1).strip() if expect_match else ''
                prefix = '/automate browser' if browser_mode else '/automate'
                self.handle_command(f'{prefix} every {interval} seconds check {url}' + (f' expect "{expected_text}"' if expected_text else ''), request_id)
                return
            if command == '/automate' or command.startswith('/automate '):
                rest = command[10:].strip() if command.startswith('/automate ') else ''
                if not rest:
                    emit({'type': 'status', 'message': 'Usage: /automate every <n> <unit> check <url>', 'request_id': request_id})
                    return
                browser_mode = False
                if rest.lower().startswith('browser '):
                    browser_mode = True
                    rest = rest[8:].strip()
                if rest == 'list' or rest.startswith('list '):
                    filter_mode = rest[5:].strip().lower() if rest.startswith('list ') else ''
                    try:
                        from automation_runtime import list_automations
                        items = list_automations(STATE_DIR)
                        if filter_mode == 'cron':
                            items = [a for a in items if str((a.get('schedule') or {}).get('type') or '').lower() == 'cron']
                        elif filter_mode == 'continuous':
                            items = [a for a in items if str(a.get('mode') or '').lower() == 'continuous']
                        elif filter_mode == 'scheduled':
                            items = [a for a in items if str(a.get('mode') or '').lower() == 'scheduled']
                        if not items:
                            emit({'type': 'status', 'message': 'No automations found.' if not filter_mode else f'No {filter_mode} automations found.', 'request_id': request_id})
                            return
                        lines = ['Automations:']
                        for a in items[:40]:
                            sched = a.get('schedule') or {}
                            sched_desc = ''
                            if str(a.get('mode') or '') == 'continuous':
                                sched_desc = f'continuous/{sched.get("poll_seconds") or (a.get("execution_policy") or {}).get("poll_seconds") or 60}s'
                            elif str(sched.get('type') or '') == 'cron':
                                sched_desc = f'cron {sched.get("cron")}'
                            else:
                                sched_desc = f'every {sched.get("interval_seconds") or 0}s'
                            lines.append(
                                f'- {a.get("automation_id")} | {a.get("title")} | '
                                f'{a.get("status")}/{a.get("health")} | {sched_desc} | '
                                f'next={a.get("next_run_at") or "continuous"}'
                            )
                        emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                    except Exception as e:
                        emit({'type': 'error', 'error': f'Automation list failed: {e}', 'request_id': request_id})
                    return
                if rest.startswith('status '):
                    automation_id = rest[7:].strip()
                    try:
                        from automation_runtime import get_automation_state
                        doc = get_automation_state(STATE_DIR, automation_id)
                        if not doc:
                            emit({'type': 'error', 'error': f'No automation found: {automation_id}', 'request_id': request_id})
                            return
                        lines = [
                            f'Automation: {doc.get("automation_id")}',
                            f'Title: {doc.get("title")}',
                            f'Status: {doc.get("status")}',
                            f'Health: {doc.get("health")}',
                            f'Next run: {doc.get("next_run_at") or "-"}',
                            f'Last result: {doc.get("last_result_summary") or "-"}',
                        ]
                        for run in doc.get('runs_tail') or []:
                            lines.append(f'- {run.get("ts")} [{"ok" if run.get("ok") else "fail"}] {run.get("summary")}')
                        emit({'type': 'status', 'message': '\n'.join(lines[:16]), 'request_id': request_id})
                    except Exception as e:
                        emit({'type': 'error', 'error': f'Automation status failed: {e}', 'request_id': request_id})
                    return
                if rest.startswith('webhook '):
                    body = rest[8:].strip()
                    parts = body.split(None, 1)
                    if len(parts) < 2:
                        emit({'type': 'error', 'error': 'Usage: /automate webhook <automation_id> <url>', 'request_id': request_id})
                        return
                    automation_id, webhook_url = parts[0].strip(), parts[1].strip()
                    try:
                        from automation_runtime import set_automation_webhook
                        doc = set_automation_webhook(STATE_DIR, automation_id, webhook_url)
                        if not doc:
                            emit({'type': 'error', 'error': f'No automation found: {automation_id}', 'request_id': request_id})
                            return
                        emit({'type': 'status', 'message': f'Updated webhook for automation {automation_id}', 'request_id': request_id})
                        emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                    except Exception as e:
                        emit({'type': 'error', 'error': f'Automation webhook update failed: {e}', 'request_id': request_id})
                    return
                for action_name, fn_name in [('pause', 'pause_automation'), ('resume', 'resume_automation'), ('stop', 'request_stop_automation')]:
                    if rest.startswith(action_name + ' '):
                        automation_id = rest[len(action_name) + 1:].strip()
                        try:
                            from automation_runtime import pause_automation, resume_automation, request_stop_automation
                            fn = {'pause': pause_automation, 'resume': resume_automation, 'stop': request_stop_automation}[action_name]
                            doc = fn(STATE_DIR, automation_id)
                            if not doc:
                                emit({'type': 'error', 'error': f'No automation found: {automation_id}', 'request_id': request_id})
                                return
                            emit({'type': 'status', 'message': f'{action_name.capitalize()}d automation {automation_id}', 'request_id': request_id})
                            emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                        except Exception as e:
                            emit({'type': 'error', 'error': f'Automation {action_name} failed: {e}', 'request_id': request_id})
                        return
                workflow_mode = False
                if rest.lower().startswith('browser-workflow '):
                    workflow_mode = True
                    rest = rest[17:].strip()
                interval = _parse_interval_phrase(rest)
                sec_match = re.search(r'\bevery\s+(\d+)\s+seconds?\b', rest, re.I)
                if sec_match:
                    interval = int(sec_match.group(1))
                cron_match = re.search(r'^cron\s+"([^"]+)"\s+check\s+', rest, re.I)
                continuous_match = re.search(r'^continuous(?:\s+every\s+(\d+)\s+seconds?)?\s+check\s+', rest, re.I)
                workflow_steps_match = re.search(r'\bsteps\s+(.+)$', rest, re.I)
                workflow_file_match = re.search(r'\bfrom\s+([^\s]+)\s*$', rest, re.I)
                if workflow_mode:
                    mode = 'scheduled'
                    schedule = {}
                    if cron_match:
                        schedule = {'type': 'cron', 'cron': cron_match.group(1).strip()}
                    elif continuous_match:
                        mode = 'continuous'
                        poll_seconds = int(continuous_match.group(1) or 60)
                        schedule = {'type': 'continuous', 'poll_seconds': poll_seconds}
                    else:
                        if interval <= 0:
                            emit({'type': 'error', 'error': 'Usage: /automate browser-workflow every <n> <unit> steps <json> | from <file>', 'request_id': request_id})
                            return
                        schedule = {'type': 'interval', 'interval_seconds': interval}
                    steps = None
                    workflow_source = ''
                    if workflow_steps_match:
                        raw_steps = workflow_steps_match.group(1).strip()
                        try:
                            parsed = json.loads(raw_steps)
                        except Exception as e:
                            emit({'type': 'error', 'error': f'Invalid workflow JSON: {e}', 'request_id': request_id})
                            return
                        steps = parsed if isinstance(parsed, list) else None
                        workflow_source = 'inline-json'
                    elif workflow_file_match:
                        workflow_path = workflow_file_match.group(1).strip().strip('"').strip("'")
                        steps = _load_workflow_steps_spec(Path(self._devop_project_root()), workflow_path)
                        workflow_source = workflow_path
                    if not isinstance(steps, list) or not steps:
                        emit({'type': 'error', 'error': 'Workflow steps must be a non-empty JSON list. Use steps <json> or from <file>.', 'request_id': request_id})
                        return
                    title = 'Browser workflow automation'
                    first_url = ''
                    for step in steps:
                        if isinstance(step, dict) and step.get('url'):
                            first_url = str(step.get('url') or '')
                            break
                    try:
                        from automation_runtime import create_automation
                        doc = create_automation(
                            STATE_DIR,
                            Path(self._devop_project_root()),
                            title=title if not first_url else f'Browser workflow: {first_url}',
                            goal=rest,
                            kind='browser_workflow',
                            mode=mode,
                            schedule=schedule,
                            action={'steps': steps, 'screenshot_on_failure': True, 'workflow_source': workflow_source},
                            created_by_agent_id=self._active_agent_id or '',
                            operation_role='monitor',
                        )
                        emit({'type': 'status', 'message': f'Started browser workflow automation {doc.get("automation_id")}\nMode: {mode}\nSource: {workflow_source or "inline-json"}\nNext run: {doc.get("next_run_at") or "continuous"}', 'request_id': request_id})
                        emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                    except Exception as e:
                        emit({'type': 'error', 'error': f'Failed to create browser workflow automation: {e}', 'request_id': request_id})
                    return
                url_match = re.search(r'(https?://\S+)', rest)
                if not url_match or 'check' not in rest.lower():
                    emit({'type': 'error', 'error': 'Usage: /automate every <n> <unit> check <url>', 'request_id': request_id})
                    return
                url = url_match.group(1).rstrip('.,)')
                expect_match = re.search(r'\bexpect\s+"([^"]+)"', rest, re.I)
                expected_text = expect_match.group(1).strip() if expect_match else ''
                mode = 'scheduled'
                schedule = {}
                if cron_match:
                    schedule = {'type': 'cron', 'cron': cron_match.group(1).strip()}
                elif continuous_match:
                    mode = 'continuous'
                    poll_seconds = int(continuous_match.group(1) or 60)
                    schedule = {'type': 'continuous', 'poll_seconds': poll_seconds}
                else:
                    if interval <= 0:
                        emit({'type': 'error', 'error': 'Usage: /automate every <n> <unit> check <url>', 'request_id': request_id})
                        return
                    schedule = {'type': 'interval', 'interval_seconds': interval}
                try:
                    from automation_runtime import create_automation
                    doc = create_automation(
                        STATE_DIR,
                        Path(self._devop_project_root()),
                        title=(f'Browser monitor: {url}' if browser_mode else f'Website monitor: {url}'),
                        goal=rest,
                        kind=('browser_check' if browser_mode else 'http_check'),
                        mode=mode,
                        schedule=schedule,
                        action={'url': url, 'method': 'GET', 'timeout_seconds': 20, 'expected_text': expected_text, 'screenshot_on_failure': True},
                        created_by_agent_id=self._active_agent_id or '',
                        operation_role='monitor',
                    )
                    emit({'type': 'status', 'message': f'Started automation {doc.get("automation_id")} for {url}\nMode: {mode}\nNext run: {doc.get("next_run_at") or "continuous"}', 'request_id': request_id})
                    emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': f'Failed to create automation: {e}', 'request_id': request_id})
                return
            if command == '/add-remote' or command.startswith('/add-remote '):
                rest = command[12:].strip() if command.startswith('/add-remote ') else ''
                try:
                    from remote_onboard import test_ssh, check_boat_installed, deploy_boat_remote, discover_remote_agents, auto_configure_fleet, full_onboard

                    if not rest:
                        emit({'type': 'status', 'message': (
                            'Usage: /add-remote <host> [user]\n'
                            '  /add-remote 55.55.55.55 ubuntu    — test SSH and discover agents\n'
                            '  /add-remote confirm                — save discovered config\n'
                            '  /add-remote cancel                 — discard pending onboarding'
                        ), 'request_id': request_id})
                        return

                    if rest == 'cancel':
                        self._pending_remote_onboard = None
                        emit({'type': 'status', 'message': 'Remote onboarding cancelled.', 'request_id': request_id})
                        return

                    if rest == 'confirm':
                        pending = self._pending_remote_onboard
                        if not pending:
                            emit({'type': 'error', 'error': 'No pending remote onboard. Run /add-remote <host> first.', 'request_id': request_id})
                            return
                        all_agents = pending.get('agents', [])
                        if not all_agents:
                            emit({'type': 'error', 'error': 'No agents to add. Run /add-remote <host> to discover agents first.', 'request_id': request_id})
                            return
                        server = auto_configure_fleet(
                            host=pending['host'],
                            user=pending.get('user', ''),
                            agents=all_agents,
                        )
                        self._pending_remote_onboard = None
                        # Start fleet sync if not already running
                        try:
                            from fleet_sync import start_fleet_sync
                            start_fleet_sync()
                        except Exception:
                            pass
                        emit({'type': 'status', 'message': (
                            f'Added server "{server["id"]}" ({server["host"]}) with '
                            f'{len(server.get("agents", []))} agent(s) to fleet.\n'
                            f'Remote sessions will appear in F3 grid shortly.'
                        ), 'request_id': request_id})
                        emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                        return

                    # Parse host and optional user
                    parts = rest.split()
                    host = parts[0]
                    user = parts[1] if len(parts) > 1 else ''

                    # Step 1: Test SSH
                    emit({'type': 'status', 'message': f'Testing SSH to {user + "@" if user else ""}{host}...', 'request_id': request_id})
                    ssh_ok, ssh_msg = test_ssh(host, user)
                    emit({'type': 'status', 'message': ssh_msg, 'request_id': request_id})
                    if not ssh_ok:
                        if not user:
                            emit({'type': 'status', 'message': 'Tip: try with a username: /add-remote <host> <user>', 'request_id': request_id})
                        return

                    # Step 2: Check boat
                    emit({'type': 'status', 'message': 'Checking for charons-boat on remote...', 'request_id': request_id})
                    boat_ok, boat_msg = check_boat_installed(host, user)
                    if boat_ok:
                        emit({'type': 'status', 'message': f'charons-boat found: {boat_msg}', 'request_id': request_id})
                    else:
                        emit({'type': 'status', 'message': 'charons-boat not found. Deploying...', 'request_id': request_id})
                        dep_ok, dep_msg = deploy_boat_remote(host, user)
                        if dep_ok:
                            emit({'type': 'status', 'message': f'Deployed: {dep_msg}', 'request_id': request_id})
                        else:
                            emit({'type': 'status', 'message': f'Deploy failed: {dep_msg}\nContinuing with tmux discovery...', 'request_id': request_id})

                    # Step 3: Discover agents
                    emit({'type': 'status', 'message': 'Discovering agents...', 'request_id': request_id})
                    discovery = discover_remote_agents(host, user)
                    boat_agents = discovery.get('boat_sessions', [])
                    tmux_agents = discovery.get('tmux_agents', [])
                    tmux_other = discovery.get('tmux_other', [])
                    all_agents = boat_agents + tmux_agents

                    if not all_agents and not tmux_other:
                        emit({'type': 'status', 'message': 'No agents found on remote. You can still add the server manually.', 'request_id': request_id})
                        # Save pending with empty agents so user can still confirm (creates empty server entry)
                        self._pending_remote_onboard = {'host': host, 'user': user, 'agents': []}
                        return

                    lines = [f'Found {len(all_agents)} agent(s) on {host}:']
                    for i, a in enumerate(all_agents, 1):
                        source = 'boat-wrapped' if a.get('source') == 'boat' else 'tmux session'
                        lines.append(f'  {i}. {a["name"]} ({a["type"]}) — {source}, {a["status"]}')
                    if tmux_other:
                        lines.append(f'\nAlso found {len(tmux_other)} other tmux session(s):')
                        for a in tmux_other:
                            lines.append(f'  - {a["name"]} (unrecognized agent)')
                    if tmux_agents:
                        lines.append(f'\nTmux agents will be auto-bridged via boat\'s tmux discovery.')
                    lines.append(f'\nType /add-remote confirm to save, or /add-remote cancel to abort.')

                    self._pending_remote_onboard = {
                        'host': host,
                        'user': user,
                        'agents': all_agents,
                        'discovery': discovery,
                    }
                    emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': f'Remote onboarding failed: {e}', 'request_id': request_id})
                return

            if command == '/project' or command.startswith('/project '):
                rest = command[8:].strip() if command.startswith('/project ') else ''
                try:
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    registry = _load_project_registry()
                    if not rest or rest == 'list':
                        if not registry:
                            emit({'type': 'status', 'message': 'No explicit projects yet. Use /project create <name> [path]', 'request_id': request_id})
                        else:
                            lines = ['Explicit projects:']
                            current = str(onboarding.get('project') or '').strip()
                            for p in registry[:20]:
                                name = str(p.get('name') or '')
                                path = str(p.get('path') or '')
                                mark = '*' if current and path == current else ' '
                                lines.append(f' {mark} {name} — {path}')
                            emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                        emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                        return
                    if rest.startswith('create '):
                        body = rest[7:].strip()
                        if not body:
                            emit({'type': 'error', 'error': 'Usage: /project create <name> [path]', 'request_id': request_id})
                            return
                        parts = body.split(None, 1)
                        name = parts[0].strip()
                        path = parts[1].strip() if len(parts) > 1 else str(onboarding.get('project') or str(ROOT)).strip()
                        slug = _project_slug(name)
                        display_name = name.replace('-', ' ').replace('_', ' ').strip() or slug
                        existing = next((p for p in registry if str(p.get('name') or '').strip() == display_name or _project_slug(p.get('name') or '') == slug), None)
                        if existing is None:
                            registry.append({
                                'id': f'project-{slug}',
                                'name': display_name,
                                'slug': slug,
                                'path': path,
                                'description': '',
                                'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                            })
                            _save_project_registry(registry)
                            emit({'type': 'status', 'message': f'Created project {display_name} at {path}', 'request_id': request_id})
                        else:
                            emit({'type': 'status', 'message': f'Project already exists: {existing.get("name", display_name)}', 'request_id': request_id})
                        emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                        return
                    if rest.startswith('use '):
                        choice = rest[4:].strip()
                        target = next((p for p in registry if str(p.get('name') or '').strip() == choice or str(p.get('id') or '').strip() == choice or str(p.get('slug') or '').strip() == _project_slug(choice)), None)
                        if not target:
                            emit({'type': 'error', 'error': f'Unknown project: {choice}', 'request_id': request_id})
                            return
                        onboarding['project'] = str(target.get('path') or '').strip() or str(ROOT)
                        (STATE_DIR / 'onboarding.json').write_text(json.dumps(onboarding, indent=2, ensure_ascii=False))
                        emit({'type': 'status', 'message': f'Selected project {target.get("name", "project")}', 'request_id': request_id})
                        emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                        return
                    emit({'type': 'error', 'error': 'Usage: /project [list|create|use]', 'request_id': request_id})
                    return
                except Exception as e:
                    emit({'type': 'error', 'error': f'Project command failed: {e}', 'request_id': request_id})
                    return

            if command in ('/hermes', '/pi'):
                try:
                    from external_session_launcher import launch_wrapped_session
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    agent_kind = command[1:]
                    result = launch_wrapped_session(
                        state_dir=STATE_DIR,
                        project_root=project,
                        agent_kind=agent_kind,
                    )
                    if result.get('ok'):
                        display_name = str(result.get('display_name') or agent_kind)
                        session_name = str(result.get('tmux_session') or result.get('session_name') or '')
                        self._register_owned_boat_session(session_name)
                        emit({'type': 'status', 'message': f'✓ {display_name} session created: {session_name}', 'request_id': request_id})
                        emit({'type': 'status', 'message': 'To view or interact with it, press F3 for the sessions grid.', 'request_id': request_id})
                        self.handle_refresh(request_id)
                    else:
                        emit({'type': 'error', 'error': f'Failed to create {agent_kind} session: {result.get("error", "unknown error")}', 'request_id': request_id})
                    return
                except Exception as e:
                    emit({'type': 'error', 'error': f'Failed to create external session: {e}', 'request_id': request_id})
                    return

            if command == '/conversation' or command.startswith('/conversation '):
                rest = command[13:].strip() if command.startswith('/conversation ') else ''
                try:
                    if not rest:
                        emit({'type': 'status', 'message': 'Usage: /conversation <agent-type> [peer|teacher student|debate|researcher reviewer|strategist critic|planner critic|architect reviewer|optimist skeptic|pair-programmers|dialogue|<count>] <topic>', 'request_id': request_id})
                        return
                    parts = rest.split()
                    provider = (parts[0] if parts else '').strip().lower()
                    from conversation_participants import get_conversation_adapter
                    adapter = get_conversation_adapter(provider)
                    if not adapter:
                        emit({'type': 'error', 'error': f'Unsupported conversation provider for now: {provider}', 'request_id': request_id})
                        return
                    if not adapter.capabilities.can_spawn:
                        emit({'type': 'error', 'error': f'Conversation spawning is not wired yet for: {provider}', 'request_id': request_id})
                        return
                    roles: list[str] = []
                    topic = ''
                    runner_mode = 'teacher-student'
                    if len(parts) >= 4 and parts[1].lower() == 'teacher' and parts[2].lower() == 'student':
                        roles = ['teacher', 'student']
                        topic = ' '.join(parts[3:]).strip()
                        runner_mode = 'teacher-student'
                    elif len(parts) >= 3 and parts[1].lower() in ('peer', 'dialogue', 'discuss'):
                        roles = ['peer-1', 'peer-2']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'peer'
                    elif len(parts) >= 4 and parts[1].lower() == 'researcher' and parts[2].lower() == 'reviewer':
                        roles = ['researcher', 'reviewer']
                        topic = ' '.join(parts[3:]).strip()
                        runner_mode = 'researcher-reviewer'
                    elif len(parts) >= 3 and parts[1].lower() in ('research', 'researcher-reviewer'):
                        roles = ['researcher', 'reviewer']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'researcher-reviewer'
                    elif len(parts) >= 3 and parts[1].lower() in ('pair-programmers', 'pair-programming', 'pair-programmer'):
                        roles = ['driver', 'navigator']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'pair-programmers'
                    elif len(parts) >= 4 and parts[1].lower() == 'pair' and parts[2].lower() in ('programmers', 'programming'):
                        roles = ['driver', 'navigator']
                        topic = ' '.join(parts[3:]).strip()
                        runner_mode = 'pair-programmers'
                    elif len(parts) >= 4 and parts[1].lower() == 'strategist' and parts[2].lower() == 'critic':
                        roles = ['strategist', 'critic']
                        topic = ' '.join(parts[3:]).strip()
                        runner_mode = 'strategist-critic'
                    elif len(parts) >= 3 and parts[1].lower() in ('strategist-critic', 'strategy-critique'):
                        roles = ['strategist', 'critic']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'strategist-critic'
                    elif len(parts) >= 4 and parts[1].lower() == 'planner' and parts[2].lower() == 'critic':
                        roles = ['planner', 'critic']
                        topic = ' '.join(parts[3:]).strip()
                        runner_mode = 'planner-critic'
                    elif len(parts) >= 3 and parts[1].lower() in ('planner-critic', 'planning-critique'):
                        roles = ['planner', 'critic']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'planner-critic'
                    elif len(parts) >= 4 and parts[1].lower() == 'architect' and parts[2].lower() == 'reviewer':
                        roles = ['architect', 'reviewer']
                        topic = ' '.join(parts[3:]).strip()
                        runner_mode = 'architect-reviewer'
                    elif len(parts) >= 3 and parts[1].lower() in ('architect-reviewer', 'architecture-review'):
                        roles = ['architect', 'reviewer']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'architect-reviewer'
                    elif len(parts) >= 4 and parts[1].lower() == 'optimist' and parts[2].lower() == 'skeptic':
                        roles = ['optimist', 'skeptic']
                        topic = ' '.join(parts[3:]).strip()
                        runner_mode = 'optimist-skeptic'
                    elif len(parts) >= 3 and parts[1].lower() in ('optimist-skeptic', 'optimism-skepticism'):
                        roles = ['optimist', 'skeptic']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'optimist-skeptic'
                    elif len(parts) >= 3 and parts[1].lower() in ('debate',):
                        roles = ['advocate', 'opposition']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'debate'
                    elif len(parts) >= 3 and str(parts[1]).isdigit():
                        count = max(2, int(parts[1]))
                        roles = ['peer-1', 'peer-2'] if count == 2 else [f'peer-{idx+1}' for idx in range(count)]
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'peer'
                    else:
                        roles = ['teacher', 'student']
                        topic = ' '.join(parts[1:]).strip()
                        runner_mode = 'teacher-student'
                    topic = topic or 'open discussion'
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    if runner_mode == 'dialogue':
                        participants = [
                            {'id': f'hermes-{idx+1}', 'role': role, 'name': f'Hermes {idx+1}'}
                            for idx, role in enumerate(roles)
                        ]
                    else:
                        participants = [
                            {'id': f'hermes-{idx+1}', 'role': role, 'name': f'Hermes {idx+1}'}
                            for idx, role in enumerate(roles)
                        ]
                    self._create_conversation_room(
                        agent_type=provider,
                        kind='conversation',
                        title=topic,
                        project=project,
                        participants=participants,
                        meta={'provider': provider, 'count': len(participants), 'topic': topic, 'conversation_mode': runner_mode},
                        request_id=request_id,
                        start_runner=True,
                        runner_mode=runner_mode,
                    )
                    return
                except Exception as e:
                    emit({'type': 'error', 'error': f'Conversation command failed: {e}', 'request_id': request_id})
                    return

            if command == '/team' or command.startswith('/team '):
                rest = command[5:].strip() if command.startswith('/team ') else ''
                try:
                    if not rest:
                        emit({'type': 'status', 'message': 'Usage: /team <agent-type> <count> <topic>', 'request_id': request_id})
                        return
                    parts = rest.split(None, 2)
                    provider = (parts[0] if len(parts) > 0 else '').strip().lower()
                    count = int(parts[1]) if len(parts) > 1 and str(parts[1]).isdigit() else 2
                    topic = (parts[2] if len(parts) > 2 else '').strip() or 'open discussion'
                    from conversation_participants import get_conversation_adapter
                    adapter = get_conversation_adapter(provider)
                    if not adapter:
                        emit({'type': 'error', 'error': f'Unsupported team provider for now: {provider}', 'request_id': request_id})
                        return
                    if not adapter.capabilities.can_spawn:
                        emit({'type': 'error', 'error': f'Team spawning is not wired yet for: {provider}', 'request_id': request_id})
                        return

                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    participants = []
                    for idx in range(count):
                        role = f'peer-{idx+1}' if count > 2 else ('peer-1' if idx == 0 else 'peer-2')
                        participants.append({'id': f'hermes-{idx+1}', 'role': role, 'name': f'Hermes {idx+1}'})
                    self._create_conversation_room(
                        agent_type=provider,
                        kind='conversation',
                        title=topic,
                        project=project,
                        participants=participants,
                        meta={'provider': provider, 'count': count, 'topic': topic, 'conversation_mode': 'peer'},
                        request_id=request_id,
                        start_runner=True,
                        runner_mode='peer',
                    )
                    return
                except Exception as e:
                    emit({'type': 'error', 'error': f'Team command failed: {e}', 'request_id': request_id})
                    return

            if command == '/devteam' or command.startswith('/devteam '):
                rest = command[8:].strip() if command.startswith('/devteam ') else ''
                try:
                    if not rest:
                        emit({'type': 'status', 'message': 'Usage: /devteam <agent-type> <count> <goal>', 'request_id': request_id})
                        return
                    parts = rest.split(None, 2)
                    provider = (parts[0] if len(parts) > 0 else '').strip().lower()
                    count = int(parts[1]) if len(parts) > 1 and str(parts[1]).isdigit() else 2
                    goal = (parts[2] if len(parts) > 2 else '').strip() or 'engineering task'
                    from conversation_participants import get_conversation_adapter
                    adapter = get_conversation_adapter(provider)
                    if not adapter:
                        emit({'type': 'error', 'error': f'Unsupported devteam provider for now: {provider}', 'request_id': request_id})
                        return
                    if not adapter.capabilities.can_spawn:
                        emit({'type': 'error', 'error': f'Devteam spawning is not wired yet for: {provider}', 'request_id': request_id})
                        return
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    participants = [
                        {'id': f'hermes-{idx+1}', 'role': 'developer', 'name': f'Hermes {idx+1}'}
                        for idx in range(count)
                    ]
                    self._create_conversation_room(
                        agent_type=provider,
                        kind='devteam',
                        title=goal,
                        project=project,
                        participants=participants,
                        meta={'provider': provider, 'count': count, 'goal': goal, 'team_mode': 'devteam'},
                        request_id=request_id,
                        start_runner=False,
                    )
                    return
                except Exception as e:
                    emit({'type': 'error', 'error': f'Devteam command failed: {e}', 'request_id': request_id})
                    return

            if command == '/pause-room' or command.startswith('/pause-room '):
                room_id = command[12:].strip() if command.startswith('/pause-room ') else ''
                try:
                    if not room_id:
                        emit({'type': 'status', 'message': 'Usage: /pause-room <room-id>', 'request_id': request_id})
                        return
                    from inter_agent_rooms import append_event, load_room, update_room
                    room = load_room(STATE_DIR, room_id)
                    if not room:
                        emit({'type': 'error', 'error': f'Unknown room: {room_id}', 'request_id': request_id})
                        return
                    update_room(STATE_DIR, room_id, status='paused', summary=f'Paused room {room_id}')
                    append_event(STATE_DIR, room_id, {'type': 'room_paused', 'summary': f'Paused room {room_id}'})
                    emit({'type': 'status', 'message': f'Paused room: {room_id}', 'request_id': request_id})
                    self.handle_refresh(request_id)
                    return
                except Exception as e:
                    emit({'type': 'error', 'error': f'Pause room failed: {e}', 'request_id': request_id})
                    return

            if command == '/resume-room' or command.startswith('/resume-room '):
                room_id = command[13:].strip() if command.startswith('/resume-room ') else ''
                try:
                    if not room_id:
                        emit({'type': 'status', 'message': 'Usage: /resume-room <room-id>', 'request_id': request_id})
                        return
                    from inter_agent_rooms import append_event, load_room, update_room
                    room = load_room(STATE_DIR, room_id)
                    if not room:
                        emit({'type': 'error', 'error': f'Unknown room: {room_id}', 'request_id': request_id})
                        return
                    update_room(STATE_DIR, room_id, status='active', summary=f'Resumed room {room_id}')
                    append_event(STATE_DIR, room_id, {'type': 'room_resumed', 'summary': f'Resumed room {room_id}'})
                    participants = list(room.get('participants') or [])
                    if len(participants) >= 2:
                        self._start_conversation_room_runner(
                            room_id,
                            str(room.get('title') or room_id),
                            participants,
                            mode=self._room_runner_mode(room),
                        )
                    emit({'type': 'status', 'message': f'Resumed room: {room_id}', 'request_id': request_id})
                    self.handle_refresh(request_id)
                    return
                except Exception as e:
                    emit({'type': 'error', 'error': f'Resume room failed: {e}', 'request_id': request_id})
                    return

            if command == '/say-room' or command.startswith('/say-room '):
                rest = command[10:].strip() if command.startswith('/say-room ') else ''
                try:
                    if not rest:
                        emit({'type': 'status', 'message': 'Usage: /say-room <room-id> <message>', 'request_id': request_id})
                        return
                    parts = shlex.split(rest)
                    if len(parts) < 2:
                        emit({'type': 'status', 'message': 'Usage: /say-room <room-id> <message>', 'request_id': request_id})
                        return
                    room_id = parts[0]
                    message = ' '.join(parts[1:]).strip()
                    if self._dispatch_libris_room_intervention(room_id, target='whole', when='now', message=message, request_id=request_id, mode='say'):
                        return
                    from inter_agent_rooms import append_event, load_room, queue_injection
                    room = load_room(STATE_DIR, room_id)
                    if not room:
                        emit({'type': 'error', 'error': f'Unknown room: {room_id}', 'request_id': request_id})
                        return
                    item = queue_injection(STATE_DIR, room_id, message=message, target='whole', when='now', sender='user')
                    if not item:
                        emit({'type': 'error', 'error': f'Failed to send room message for: {room_id}', 'request_id': request_id})
                        return
                    append_event(STATE_DIR, room_id, {
                        'type': 'room_message_sent',
                        'target': 'whole',
                        'summary': message[:240],
                        'message': message,
                    })
                    if str(room.get('status') or 'active') == 'active' and len(list(room.get('participants') or [])) >= 2:
                        self._start_conversation_room_runner(
                            room_id,
                            str(room.get('title') or room_id),
                            list(room.get('participants') or []),
                            mode=self._room_runner_mode(room),
                        )
                    emit({'type': 'status', 'message': f'Sent room message to {room_id}: {message[:120]}', 'request_id': request_id})
                    self.handle_refresh(request_id)
                    return
                except Exception as e:
                    emit({'type': 'error', 'error': f'Say room failed: {e}', 'request_id': request_id})
                    return

            if command == '/inject-room' or command.startswith('/inject-room '):
                rest = command[13:].strip() if command.startswith('/inject-room ') else ''
                try:
                    if not rest:
                        emit({'type': 'status', 'message': 'Usage: /inject-room <room-id> [--target whole|teacher|student|<participant>|coordinator|topic:<slug>|node:<agent-id>|researcher:<slug>|judge:<slug>|shade:<agent-id>] [--when now|next] <message>', 'request_id': request_id})
                        return
                    parts = shlex.split(rest)
                    if not parts:
                        emit({'type': 'status', 'message': 'Usage: /inject-room <room-id> [--target whole|teacher|student|<participant>|coordinator|topic:<slug>|node:<agent-id>|researcher:<slug>|judge:<slug>|shade:<agent-id>] [--when now|next] <message>', 'request_id': request_id})
                        return
                    room_id = parts[0]
                    target = 'whole'
                    when = 'next'
                    idx = 1
                    while idx < len(parts):
                        token = parts[idx]
                        if token == '--target' and idx + 1 < len(parts):
                            target = parts[idx + 1]
                            idx += 2
                            continue
                        if token == '--when' and idx + 1 < len(parts):
                            when = parts[idx + 1]
                            idx += 2
                            continue
                        break
                    message = ' '.join(parts[idx:]).strip()
                    if not message:
                        emit({'type': 'error', 'error': 'Injection message cannot be empty.', 'request_id': request_id})
                        return
                    if self._dispatch_libris_room_intervention(room_id, target=target, when=when, message=message, request_id=request_id, mode='inject'):
                        return
                    from inter_agent_rooms import append_event, load_room, queue_injection
                    room = load_room(STATE_DIR, room_id)
                    if not room:
                        emit({'type': 'error', 'error': f'Unknown room: {room_id}', 'request_id': request_id})
                        return
                    item = queue_injection(STATE_DIR, room_id, message=message, target=target, when=when, sender='user')
                    if not item:
                        emit({'type': 'error', 'error': f'Failed to queue injection for room: {room_id}', 'request_id': request_id})
                        return
                    append_event(STATE_DIR, room_id, {
                        'type': 'room_injection_requested',
                        'target': target,
                        'when': when,
                        'summary': message[:240],
                    })
                    if str(room.get('status') or 'active') == 'active' and len(list(room.get('participants') or [])) >= 2:
                        self._start_conversation_room_runner(
                            room_id,
                            str(room.get('title') or room_id),
                            list(room.get('participants') or []),
                            mode=self._room_runner_mode(room),
                        )
                    emit({'type': 'status', 'message': f'Queued room injection for {room_id} target={target} when={when}: {message[:120]}', 'request_id': request_id})
                    self.handle_refresh(request_id)
                    return
                except Exception as e:
                    emit({'type': 'error', 'error': f'Inject room failed: {e}', 'request_id': request_id})
                    return

            if command == '/delete-room' or command.startswith('/delete-room '):
                room_id = command[12:].strip() if command.startswith('/delete-room ') else ''
                try:
                    if not room_id:
                        emit({'type': 'status', 'message': 'Usage: /delete-room <room-id>', 'request_id': request_id})
                        return
                    from inter_agent_rooms import delete_room, load_room
                    room = load_room(STATE_DIR, room_id)
                    participant_sessions = list(room.get('participant_sessions') or []) if room else []
                    if not participant_sessions and room:
                        participant_sessions = [p.get('session') for p in (room.get('participants') or []) if p.get('session')]
                    terminated: list[str] = []
                    for session_name in participant_sessions:
                        if session_name and _terminate_boat_session(str(session_name)):
                            terminated.append(str(session_name))
                            self._owned_boat_sessions.discard(str(session_name))
                    if delete_room(STATE_DIR, room_id):
                        msg = f'Deleted room record: {room_id}'
                        if terminated:
                            msg += '\nClosed sessions: ' + ', '.join(terminated)
                        emit({'type': 'status', 'message': msg, 'request_id': request_id})
                        self.handle_refresh(request_id)
                    else:
                        emit({'type': 'error', 'error': f'Could not delete room record: {room_id}', 'request_id': request_id})
                    return
                except Exception as e:
                    emit({'type': 'error', 'error': f'Delete room failed: {e}', 'request_id': request_id})
                    return

            if command == '/resume' or command.startswith('/resume '):
                arg = command[8:].strip() if command.startswith('/resume ') else ''
                try:
                    from conversation_store import list_conversations, load_conversation, dict_to_message, message_to_dict
                    convos = list_conversations(STATE_DIR)
                    if arg:
                        # Direct resume — must reset the engine so it gets the
                        # correct agent_id for the target session.
                        self._active_agent_id = arg
                        self.engine = None  # force re-creation with new agent_id
                        engine, _ = self._ensure_engine()
                        restored_count = 0
                        saved = None

                        # Try lossless store first — full raw history.
                        # Query with arg directly in case engine.agent_id differs.
                        store_msgs = _full_messages_from_store(arg)
                        if store_msgs:
                            restored_count = len(store_msgs)
                            if engine:
                                engine.messages = list(store_msgs)
                            saved = [message_to_dict(m) for m in store_msgs]

                        if not restored_count:
                            # Fall back to JSONL
                            saved = _sanitize_saved_messages(load_conversation(STATE_DIR, arg))
                            if saved and engine:
                                msgs = [dict_to_message(m) for m in saved]
                                engine.messages = msgs
                                # Migrate into lossless store
                                if engine.has_lossless_store:
                                    engine.import_into_store(msgs)

                        if saved:
                            self._load_tasks_from_ledger(arg)
                            emit({
                                'type': 'conversation_restored',
                                'messages': saved,
                                'count': len(saved),
                                'agent_id': arg,
                            })
                            # Push session info so task pane updates immediately
                            emit({
                                'type': 'refresh',
                                'payload': {'session_info': self._get_session_info()},
                            })
                        else:
                            emit({'type': 'error', 'error': f'No saved conversation for {arg}', 'request_id': request_id})
                    elif convos:
                        # Show session picker with last user message preview
                        import time

                        # Pre-load SQLite counts + last user messages for accuracy
                        _store_info = {}
                        try:
                            from context_store import ContextStore
                            from store_adapter import get_db
                            _sdb = get_db(STATE_DIR)
                            # Batch: get counts per agent
                            for row in _sdb.fetchall(
                                "SELECT agent_id, COUNT(*) as cnt FROM conversation_messages GROUP BY agent_id"
                            ):
                                _store_info[row['agent_id']] = {'count': row['cnt'], 'preview': ''}
                            # Batch: get last user message per agent (for preview)
                            for aid, info in _store_info.items():
                                row = _sdb.fetchone(
                                    "SELECT content FROM conversation_messages "
                                    "WHERE agent_id = ? AND role = 'user' AND content != '' "
                                    "ORDER BY seq DESC LIMIT 1",
                                    (aid,),
                                )
                                if row and row['content']:
                                    first_line = row['content'].strip().split('\n')[0]
                                    info['preview'] = first_line[:60] + ('…' if len(first_line) > 60 else '')
                        except Exception:
                            pass

                        items = []
                        for c in sorted(convos, key=lambda x: x.get('last_timestamp', 0), reverse=True):
                            age = ''
                            if c.get('last_timestamp'):
                                secs = time.time() - c['last_timestamp']
                                if secs < 60: age = f'{int(secs)}s ago'
                                elif secs < 3600: age = f'{int(secs/60)}m ago'
                                elif secs < 86400: age = f'{int(secs/3600)}h ago'
                                else: age = f'{int(secs/86400)}d ago'
                            # Use SQLite info when available and more complete
                            aid = c['agent_id']
                            si = _store_info.get(aid)
                            msg_count = c.get('message_count', 0)
                            preview = ''
                            if si and si['count'] >= msg_count:
                                msg_count = si['count']
                                preview = si.get('preview', '')
                            if not preview:
                                try:
                                    saved = load_conversation(STATE_DIR, aid)
                                    for msg in reversed(saved):
                                        if msg.get('role') == 'user' and msg.get('content', '').strip():
                                            first_line = msg['content'].strip().split('\n')[0]
                                            preview = first_line[:60]
                                            if len(first_line) > 60:
                                                preview += '…'
                                            break
                                except Exception:
                                    pass
                            items.append({
                                'id': aid,
                                'desc': f"{preview or '(no messages)'}",
                                'age': f"{msg_count}msg  {age}",
                            })
                        if items:
                            emit({
                                'type': 'model_picker',
                                'models': items,
                                'provider': 'resume',
                                'request_id': request_id,
                            })
                        else:
                            emit({'type': 'status', 'message': 'No other saved conversations found.', 'request_id': request_id})
                    else:
                        emit({'type': 'status', 'message': 'No saved conversations found.', 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': f'Resume failed: {e}', 'request_id': request_id})
                return
            if command == '/hotkeys':
                emit({
                    'type': 'suggestions',
                    'title': 'Keyboard Shortcuts',
                    'items': [
                        {'cmd': 'F1', 'desc': 'Switch to Chat view'},
                        {'cmd': 'F2', 'desc': 'Switch to Dashboard view'},
                        {'cmd': 'F3', 'desc': 'Switch to Session Grid view'},
                        {'cmd': 'F4', 'desc': 'Switch to Room Controls view'},
                        {'cmd': 'd', 'desc': 'F4 Rooms: delete selected room and close its participant sessions'},
                        {'cmd': 'Tab', 'desc': 'Dashboard: switch agents/projects | Sessions: cycle panes'},
                        {'cmd': '↑↓', 'desc': 'Navigate lists, menus, grid'},
                        {'cmd': '←→', 'desc': 'Navigate session grid horizontally'},
                        {'cmd': 'Enter', 'desc': 'Select menu item / enter session / submit input'},
                        {'cmd': 'Escape', 'desc': 'Close menu / exit session'},
                        {'cmd': 'Ctrl+F', 'desc': 'Zoom/unzoom session in grid'},
                        {'cmd': 'Ctrl+T', 'desc': 'Toggle timestamps'},
                        {'cmd': 'Ctrl+Y', 'desc': 'Toggle visible thoughts'},
                        {'cmd': 'Ctrl+C', 'desc': 'Exit Charon'},
                        {'cmd': '/', 'desc': 'Open command menu'},
                    ],
                    'request_id': request_id,
                })
                return
            if command == '/timestamps':
                emit({'type': 'toggle_timestamps', 'request_id': request_id})
                return
            if command in ('/interrupt', '/abort'):
                self.handle_abort(request_id)
                return
            if command == '/thoughts':
                self.visible_thoughts = not self.visible_thoughts
                try:
                    settings = _load_ui_settings()
                    settings['visible_thoughts'] = self.visible_thoughts
                    _save_ui_settings(settings)
                except Exception:
                    pass
                emit({
                    'type': 'toggle_visible_thoughts',
                    'enabled': self.visible_thoughts,
                    'supported': self._thoughts_supported(),
                    'provider': self._current_provider_name(),
                    'request_id': request_id,
                })
                return
            if command.startswith('/setup shade-model '):
                model_name = command[18:].strip()
                if model_name == 'same':
                    from model_registry import load_registry, save_registry
                    reg = load_registry(STATE_DIR)
                    reg['shade_model_mode'] = 'same'
                    save_registry(STATE_DIR, reg)
                    emit({'type': 'status', 'message': 'Shade model: same as main agent.', 'request_id': request_id})
                elif model_name == 'auto':
                    from model_registry import load_registry, save_registry
                    reg = load_registry(STATE_DIR)
                    reg['shade_model_mode'] = 'auto'
                    save_registry(STATE_DIR, reg)
                    emit({'type': 'status', 'message': 'Shade model: auto (Charon picks per task).', 'request_id': request_id})
                else:
                    from model_registry import load_registry, save_registry
                    reg = load_registry(STATE_DIR)
                    reg['shade_model_mode'] = 'fixed'
                    # Parse provider/model format
                    if '/' in model_name:
                        parts = model_name.split('/', 1)
                        reg['shade_provider'] = parts[0]
                        reg['shade_model'] = parts[1]
                    else:
                        reg['shade_model'] = model_name
                    save_registry(STATE_DIR, reg)
                    emit({'type': 'status', 'message': f'Shade model: {model_name}', 'request_id': request_id})
                return
            if command.startswith('/setup shade-url '):
                url = command[17:].strip()
                from model_registry import load_registry, save_registry
                reg = load_registry(STATE_DIR)
                reg['shade_base_url'] = url
                save_registry(STATE_DIR, reg)
                emit({'type': 'status', 'message': f'Shade base URL: {url}', 'request_id': request_id})
                return
            if command.startswith('/setup shade-key '):
                key = command[17:].strip()
                from model_registry import load_registry, save_registry
                reg = load_registry(STATE_DIR)
                reg['shade_api_key'] = key
                save_registry(STATE_DIR, reg)
                emit({'type': 'status', 'message': 'Shade API key saved.', 'request_id': request_id})
                return
            if command == '/models' or command == '/models list':
                try:
                    import httpx
                    lines = []

                    # Local models (LM Studio / Ollama)
                    for name, url in [('LM Studio', 'http://127.0.0.1:1234/v1'), ('Ollama', 'http://127.0.0.1:11434/v1')]:
                        try:
                            resp = httpx.get(f'{url}/models', timeout=3)
                            if resp.status_code == 200:
                                data = resp.json()
                                models = [m.get('id', '?') for m in data.get('data', [])]
                                lines.append(f'{name} ({url}):')
                                for m in models:
                                    lines.append(f'  {m}')
                                lines.append('')
                        except Exception:
                            pass

                    # Current config
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    current = onboarding.get('model') or onboarding.get('provider_model') or 'none'
                    lines.append(f'Current model: {current}')
                    lines.append(f'Provider: {onboarding.get("provider", "none")}')

                    try:
                        from model_registry import load_registry
                        reg = load_registry(STATE_DIR)
                        shade_model = reg.get('shade_model') or '(same as main)'
                        lines.append(f'Shade model: {shade_model}')
                    except Exception:
                        pass

                    if lines:
                        emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                    else:
                        emit({'type': 'status', 'message': 'No local model servers detected.', 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/settings' or command == '/config':
                try:
                    lines = ['# Charon Settings', '']

                    # Provider
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    provider = str(onboarding.get('provider') or 'none')
                    model = str(onboarding.get('model') or onboarding.get('provider_model') or 'none')
                    project = str(onboarding.get('project') or 'none')
                    lines.append(f'Provider: {provider}')
                    lines.append(f'Model: {model}')
                    lines.append(f'Project: {project}')
                    lines.append(f'Setup complete: {onboarding.get("complete", False)}')
                    lines.append('')

                    # Shade model
                    try:
                        from model_registry import load_registry
                        reg = load_registry(STATE_DIR)
                        shade_mode = reg.get('shade_model_mode', 'auto')
                        shade_model = reg.get('shade_model') or '(same as main)'
                        shade_provider = reg.get('shade_provider') or '(same as main)'
                        shade_url = reg.get('shade_base_url') or '(default)'
                        lines.append(f'Shade model mode: {shade_mode}')
                        lines.append(f'Shade model: {shade_model}')
                        lines.append(f'Shade provider: {shade_provider}')
                        lines.append(f'Shade URL: {shade_url}')
                        lines.append('Shade model is also used for lightweight orchestration/NL parsing fallback.')
                    except Exception:
                        lines.append('Shade model: (not configured)')
                    lines.append('')

                    # Autonomous mode
                    try:
                        from autonomous import load_autonomous_config
                        auto = load_autonomous_config(STATE_DIR)
                        lines.append(f'Autonomous mode: {"ON" if auto.get("enabled") else "OFF"}')
                        tb = auto.get('time_budget_minutes')
                        lines.append(f'Time budget: {tb} min' if tb else 'Time budget: unlimited')
                        lines.append(f'Git checkpoints: {"on" if auto.get("git_checkpoint") else "off"}')
                    except Exception:
                        lines.append('Autonomous mode: OFF')
                    lines.append('')

                    # Consolidation
                    try:
                        from consolidation import load_config
                        con = load_config(STATE_DIR)
                        lines.append(f'Consolidation: {"on" if con.get("enabled") else "off"}')
                        lines.append(f'Consolidation model: {con.get("model_tier", "fast")}')
                        lines.append(f'Consolidation interval: {con.get("scan_interval_heartbeats", 50)} heartbeats')
                    except Exception:
                        lines.append('Consolidation: on (default)')
                    lines.append('')

                    # Approval
                    try:
                        from tool_approval import get_approval_status
                        status = get_approval_status(self._active_agent_id or 'default')
                        skip = status.get('skip_all', False)
                        approved = status.get('session_approved', [])
                        lines.append(f'Approval: {"DISABLED" if skip else "enabled"}')
                        if approved:
                            lines.append(f'Session approved: {", ".join(approved[:5])}')
                    except Exception:
                        lines.append('Approval: enabled')
                    lines.append('')

                    # Agent
                    try:
                        from agent_lifecycle import list_agents
                        agents = [a for a in list_agents() if a.get('role') == 'charon']
                        lines.append(f'Agents: {len(agents)}')
                        for a in agents[:5]:
                            lines.append(f'  {a.get("name", a.get("id", "?"))} ({a.get("id")}) — {a.get("status", "?")}')
                    except Exception:
                        pass
                    lines.append('')

                    # Tools
                    from tools import ALL_TOOL_DEFS
                    built_in = [t['name'] for t in ALL_TOOL_DEFS]
                    lines.append(f'Tools ({len(built_in)}): {", ".join(built_in)}')
                    try:
                        from tools.dynamic_loader import list_dynamic_tools
                        dynamic = list_dynamic_tools()
                        if dynamic:
                            lines.append(f'Dynamic tools ({len(dynamic)}): {", ".join(t["name"] for t in dynamic)}')
                    except Exception:
                        pass

                    emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/approve' or command.startswith('/approve '):
                try:
                    from tool_approval import approve_tool_for_session, approve_for_session, get_approval_status
                    arg = command[9:].strip() if command.startswith('/approve ') else ''
                    # Approve for all possible session IDs to avoid mismatch
                    session_ids = set()
                    session_ids.add(self._active_agent_id or 'default')
                    session_ids.add('default')
                    # Also add the actual agent ID from the engine
                    if self.engine and self.engine.agent_id:
                        session_ids.add(self.engine.agent_id)
                    session_id = self._active_agent_id or 'default'

                    if arg == 'status':
                        status = get_approval_status(session_id)
                        skip = '(ALL CHECKS DISABLED)' if status['skip_all'] else ''
                        lines = [f'Approval status {skip}']
                        if status['session_approved']:
                            lines.append('Session approved:')
                            for a in status['session_approved']:
                                lines.append(f'  ✓ {a}')
                        if status['permanent_approved']:
                            lines.append('Permanently approved:')
                            for a in status['permanent_approved']:
                                lines.append(f'  ✓ {a}')
                        if not status['session_approved'] and not status['permanent_approved']:
                            lines.append('  (no approvals granted)')
                        emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                    elif not arg or arg == 'all':
                        # Approve all tools for all session IDs
                        for sid in session_ids:
                            for tool in ('Web', 'Http', 'Write', 'Edit', 'Bash', 'Git', 'SpawnBatch', 'SpawnShade', 'Browser', 'X'):
                                approve_tool_for_session(sid, tool)
                        emit({'type': 'status', 'message': '✓ All tools approved for this session.', 'request_id': request_id})
                    elif arg.startswith('network') or arg.startswith('web') or arg.startswith('http'):
                        for sid in session_ids:
                            approve_tool_for_session(sid, 'Web')
                            approve_tool_for_session(sid, 'Http')
                            approve_tool_for_session(sid, 'Browser')
                            approve_tool_for_session(sid, 'X')
                        emit({'type': 'status', 'message': '✓ Network tools approved for this session.', 'request_id': request_id})
                    elif arg.startswith('write') or arg.startswith('edit') or arg.startswith('file'):
                        for sid in session_ids:
                            approve_tool_for_session(sid, 'Write')
                            approve_tool_for_session(sid, 'Edit')
                            approve_tool_for_session(sid, 'Git')
                        emit({'type': 'status', 'message': '✓ File modification tools approved for this session.', 'request_id': request_id})
                    else:
                        for sid in session_ids:
                            approve_for_session(sid, arg)
                        emit({'type': 'status', 'message': f'✓ Approved: {arg}', 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command.startswith('/provider ') or command == '/provider':
                if command == '/provider':
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    provider = str(onboarding.get('provider') or 'none')
                    model = str(onboarding.get('model') or onboarding.get('provider_model') or 'none')
                    emit({'type': 'status', 'message': f'Current provider: {provider}/{model}', 'request_id': request_id})
                else:
                    new_provider = command[10:].strip()
                    if not new_provider:
                        emit({'type': 'error', 'error': 'Usage: /provider <name>', 'request_id': request_id})
                        return

                    current_provider = self._current_provider_name()
                    if new_provider == current_provider:
                        emit({'type': 'status', 'message': f'Already on {new_provider}.', 'request_id': request_id})
                        return

                    if self._has_transferable_context():
                        self._prompt_provider_switch(new_provider, request_id, source='provider')
                    else:
                        self._switch_provider_fresh(new_provider, request_id)
                return
            if command == '/shades' or command == '/shade stats':
                try:
                    from shade_stats import get_shade_stats, format_stats
                    stats = get_shade_stats(STATE_DIR)
                    if stats:
                        emit({'type': 'status', 'message': format_stats(stats), 'request_id': request_id})
                    else:
                        emit({'type': 'status', 'message': 'No shade usage recorded yet.', 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/batch' or command.startswith('/batch '):
                batch_id = command[7:].strip() if command.startswith('/batch ') else ''
                try:
                    from batch_orchestrator import get_batch, list_batches, summarize_batch
                    if batch_id:
                        batch = get_batch(STATE_DIR, batch_id)
                        if batch:
                            lines = [summarize_batch(batch)]
                            for t in batch.get('tasks', []):
                                icon = {'completed': '✓', 'failed': '✗', 'in_progress': '◆', 'pending': '○'}.get(t.get('status', ''), '·')
                                model = t.get('model_used') or ''
                                complexity = t.get('complexity', 'normal')
                                model_tag = f' [{model}]' if model else ''
                                cx_tag = f' ({complexity})' if complexity != 'normal' else ''
                                summary = t.get('result_summary') or t.get('error') or ''
                                lines.append(f'  {icon} {t.get("title", "")}{cx_tag}{model_tag}: {summary[:50]}')
                            emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                        else:
                            emit({'type': 'error', 'error': f'Batch not found: {batch_id}', 'request_id': request_id})
                    else:
                        batches = list_batches(STATE_DIR)
                        if batches:
                            lines = [f'{len(batches)} batch(es):']
                            for b in batches[-10:]:
                                lines.append(f'  {summarize_batch(b)}')
                            emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                        else:
                            emit({'type': 'status', 'message': 'No batches.', 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/tools' or command == '/tools list':
                from tools import ALL_TOOL_DEFS
                lines = ['Built-in tools:']
                for t in ALL_TOOL_DEFS:
                    lines.append(f'  {t["name"]}: {t["description"][:60]}')
                try:
                    from tools.dynamic_loader import list_dynamic_tools, get_load_errors
                    dynamic = list_dynamic_tools()
                    if dynamic:
                        lines.append('\nDynamic tools:')
                        for t in dynamic:
                            lines.append(f'  {t["name"]}: {t["description"]}')
                            lines.append(f'    source: {t["source"]}')
                    errors = get_load_errors()
                    if errors:
                        lines.append('\nLoad errors:')
                        for e in errors:
                            lines.append(f'  {e["path"]}: {e["error"]}')
                except Exception:
                    pass
                emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                return
            if command == '/tools reload':
                try:
                    from tools.dynamic_loader import load_dynamic_tools
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    defs, executors, errors = load_dynamic_tools(STATE_DIR, Path(project))
                    msg = f'Reloaded: {len(defs)} dynamic tool(s)'
                    if errors:
                        msg += f', {len(errors)} error(s)'
                        for e in errors:
                            msg += f'\n  {e["error"]}'
                    # Refresh engine tools
                    if self.engine:
                        from tools.dynamic_loader import get_all_tool_defs
                        self.engine.tools = get_all_tool_defs(STATE_DIR, Path(project))
                    emit({'type': 'status', 'message': msg, 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': f'Reload failed: {e}', 'request_id': request_id})
                return
            if command == '/autonomous' or command == '/autonomous status':
                try:
                    from autonomous import load_autonomous_config, get_proposed_goals, get_goals_by_status
                    config = load_autonomous_config(STATE_DIR)
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    proposed = get_proposed_goals(STATE_DIR, project=project)
                    confirmed = get_goals_by_status(STATE_DIR, project=project, status='confirmed')
                    executing = get_goals_by_status(STATE_DIR, project=project, status='executing')
                    msg = (
                        f'Autonomous mode: {"ON" if config.get("enabled") else "OFF"}\n'
                        f'Time budget: {config.get("time_budget_minutes") or "unlimited"} min\n'
                        f'Token budget: {config.get("token_budget") or "unlimited"}\n'
                        f'Git checkpoints: {"on" if config.get("git_checkpoint") else "off"}\n'
                        f'Goals — proposed: {len(proposed)}, confirmed: {len(confirmed)}, executing: {len(executing)}'
                    )
                    emit({'type': 'status', 'message': msg, 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/autonomous on':
                from autonomous import load_autonomous_config, save_autonomous_config
                config = load_autonomous_config(STATE_DIR)
                config['enabled'] = True
                save_autonomous_config(STATE_DIR, config)
                emit({'type': 'status', 'message': 'Autonomous mode ON. Agent will self-assign from confirmed goals.', 'request_id': request_id})
                return
            if command == '/autonomous off':
                from autonomous import load_autonomous_config, save_autonomous_config
                config = load_autonomous_config(STATE_DIR)
                config['enabled'] = False
                save_autonomous_config(STATE_DIR, config)
                emit({'type': 'status', 'message': 'Autonomous mode OFF.', 'request_id': request_id})
                return
            if command.startswith('/autonomous time '):
                try:
                    minutes = int(command[16:].strip())
                    from autonomous import load_autonomous_config, save_autonomous_config
                    config = load_autonomous_config(STATE_DIR)
                    config['time_budget_minutes'] = minutes
                    save_autonomous_config(STATE_DIR, config)
                    emit({'type': 'status', 'message': f'Time budget set to {minutes} minutes.', 'request_id': request_id})
                except ValueError:
                    emit({'type': 'error', 'error': 'Usage: /autonomous time <minutes>', 'request_id': request_id})
                return
            if command.startswith('/autonomous tokens '):
                try:
                    tokens = int(command[18:].strip())
                    from autonomous import load_autonomous_config, save_autonomous_config
                    config = load_autonomous_config(STATE_DIR)
                    config['token_budget'] = tokens
                    save_autonomous_config(STATE_DIR, config)
                    emit({'type': 'status', 'message': f'Token budget set to {tokens}.', 'request_id': request_id})
                except ValueError:
                    emit({'type': 'error', 'error': 'Usage: /autonomous tokens <count>', 'request_id': request_id})
                return
            if command == '/confirm' or command.startswith('/confirm '):
                goal_id = command[9:].strip() if command.startswith('/confirm ') else ''
                try:
                    from autonomous import confirm_goal, get_proposed_goals
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    if not goal_id:
                        proposed = get_proposed_goals(STATE_DIR, project=project)
                        if proposed:
                            goal_id = proposed[0].get('goal_id', '')
                        else:
                            emit({'type': 'status', 'message': 'No proposed goals to confirm.', 'request_id': request_id})
                            return
                    result = confirm_goal(STATE_DIR, project=project, goal_id=goal_id)
                    if result:
                        emit({'type': 'status', 'message': f'Goal confirmed: {result.get("title", "")[:80]}', 'request_id': request_id})
                        emit({'type': 'refresh', 'payload': {'session_info': self._get_session_info()}, 'request_id': request_id})
                    else:
                        emit({'type': 'error', 'error': f'Goal not found: {goal_id}', 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/reject' or command.startswith('/reject '):
                goal_id = command[8:].strip() if command.startswith('/reject ') else ''
                try:
                    from autonomous import reject_goal, get_proposed_goals
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    if not goal_id:
                        proposed = get_proposed_goals(STATE_DIR, project=project)
                        if proposed:
                            goal_id = proposed[0].get('goal_id', '')
                    if goal_id:
                        reject_goal(STATE_DIR, project=project, goal_id=goal_id)
                        emit({'type': 'status', 'message': f'Goal rejected (moved to backlog).', 'request_id': request_id})
                        emit({'type': 'refresh', 'payload': {'session_info': self._get_session_info()}, 'request_id': request_id})
                    else:
                        emit({'type': 'status', 'message': 'No proposed goals to reject.', 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/history' or command.startswith('/history '):
                agent_id = command[9:].strip() if command.startswith('/history ') else ''
                self.handle_agent_ledger(agent_id, request_id)
                return
            if command == '/consolidation' or command == '/consolidation status':
                self.handle_consolidation_traces(request_id)
                return
            if command == '/consolidation run':
                self.handle_consolidation_run(request_id)
                return
            if command.startswith('/consolidation model '):
                model_name = command[20:].strip()
                self.handle_consolidation_config({'action': 'set', 'config': {'model_tier': model_name}}, request_id)
                return
            if command.startswith('/consolidation interval '):
                try:
                    interval = int(command[23:].strip())
                    self.handle_consolidation_config({'action': 'set', 'config': {'scan_interval_heartbeats': interval}}, request_id)
                except ValueError:
                    emit({'type': 'error', 'error': 'Interval must be a number.', 'request_id': request_id})
                return
            if command == '/consolidation off':
                self.handle_consolidation_config({'action': 'set', 'config': {'enabled': False}}, request_id)
                return
            if command == '/consolidation on':
                self.handle_consolidation_config({'action': 'set', 'config': {'enabled': True}}, request_id)
                return
            if command == '/reset':
                if self.engine:
                    self.engine.reset()
                self.chat_history = []
                emit({'type': 'status', 'message': 'Conversation cleared.', 'request_id': request_id})
                return
            if command == '/model':
                onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                model = str(onboarding.get('model') or onboarding.get('provider_model') or 'none')
                provider = str(onboarding.get('provider') or 'none')
                # Show current model and trigger picker
                emit({'type': 'status', 'message': f'Current: {provider}/{model}', 'request_id': request_id})
                self._run_setup_command('model', request_id)
                return
            if command.startswith('/model '):
                model_name = command[7:].strip()
                if model_name:
                    self._run_setup_command(f'model {model_name}', request_id)
                return
            if command == '/provider':
                onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                provider = str(onboarding.get('provider') or 'none')
                emit({'type': 'status', 'message': f'Current provider: {provider}', 'request_id': request_id})
                # Show provider picker as menu
                emit({
                    'type': 'model_picker',
                    'models': [
                        {'id': 'claude-code', 'desc': 'Anthropic Claude (OAuth)'},
                        {'id': 'codex', 'desc': 'OpenAI Codex (OAuth)'},
                        {'id': 'lmstudio', 'desc': 'Local LM Studio'},
                        {'id': 'api', 'desc': 'Custom API endpoint'},
                    ],
                    'provider': 'switch',
                    'request_id': request_id,
                })
                return
            if command.startswith('/provider '):
                provider_name = command[10:].strip()
                if provider_name:
                    self._run_setup_command(f'provider {provider_name}', request_id)
                return

            # Unknown command — show suggestions
            suggestions = self._get_suggestions(command)
            if suggestions:
                emit({
                    'type': 'suggestions',
                    'title': f'Did you mean?',
                    'items': suggestions,
                    'request_id': request_id,
                })
            else:
                emit({'type': 'error', 'error': f'Unknown command: {command}', 'request_id': request_id})
        except Exception as e:
            emit({'type': 'error', 'error': str(e), 'request_id': request_id})

    def _detect_lmstudio_models(self) -> list[str]:
        models: list[str] = []
        try:
            import httpx
            resp = httpx.get('http://127.0.0.1:1234/v1/models', timeout=3.0)
            if resp.status_code == 200:
                for m in resp.json().get('data', []):
                    mid = str(m.get('id') or '').strip()
                    if mid and mid not in models:
                        models.append(mid)
        except Exception:
            pass
        return models

    def _conversation_hermes_local_runtime(self, meta: dict | None = None) -> dict[str, str]:
        meta = dict(meta or {})
        onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
        cfg = resolve_provider_config(STATE_DIR, session_id=self._active_agent_id or None)
        base_url = str(meta.get('base_url') or os.environ.get('CHARON_LOCAL_BASE_URL') or os.environ.get('CHARON_LMSTUDIO_BASE_URL') or '').strip().rstrip('/')
        if not base_url:
            base_url = str(cfg.get('base_url') or '').strip().rstrip('/')
        if not base_url:
            base_url = 'http://127.0.0.1:1234/v1'

        model = str(meta.get('model') or '').strip()
        if not model and str(cfg.get('provider_raw') or '').strip().lower() in ('lmstudio', 'local'):
            model = str(cfg.get('model_id') or '').strip()
        if not model:
            detected = self._detect_lmstudio_models()
            if detected:
                model = detected[0]
        if not model:
            raw = str(onboarding.get('provider_model') or onboarding.get('model') or '').strip()
            candidate = raw.split('/', 1)[1] if '/' in raw else raw
            lowered = candidate.lower()
            if candidate and not lowered.startswith('claude-') and not lowered.startswith('gpt-') and lowered not in ('o3', 'o4-mini', 'o3-mini', 'codex-mini-latest'):
                model = candidate
        if not model:
            model = os.environ.get('CHARON_LOCAL_MODEL', '').strip() or 'qwen3-30b-a3b'
        return {'provider': 'lmstudio', 'base_url': base_url, 'model': model}

    def _run_setup_command(self, rest: str, request_id: str | None, *, skip_prompt: bool = False):
        """Execute setup subcommands."""
        import importlib.util
        # Directly update onboarding state
        onboarding = _load_json(STATE_DIR / 'onboarding.json', {})

        parts = rest.split(maxsplit=1)
        subcmd = parts[0] if parts else ''
        arg = parts[1].strip() if len(parts) > 1 else ''

        session_id = self._active_agent_id or None
        session_override = load_session_provider_config(STATE_DIR, session_id) if session_id else {}

        if subcmd == 'status':
            status_payload = dict(onboarding)
            if session_override:
                status_payload['session_override'] = session_override
                status_payload['session_id'] = session_id
            emit({'type': 'status', 'message': json.dumps(status_payload, indent=2), 'request_id': request_id})
        elif subcmd == 'reset':
            onboarding = {'complete': False, 'step': 'provider-mode', 'provider_mode': '', 'provider': '', 'model': ''}
            self._save_onboarding(onboarding)
            self.engine = None  # force re-creation
            emit({'type': 'status', 'message': 'Setup reset.', 'request_id': request_id})
        elif subcmd == 'provider':
            # Parse: /setup provider claude-code [--force]
            arg_parts = arg.split()
            force_oauth = '--force' in arg_parts
            arg = arg_parts[0] if arg_parts else ''
            allowed = {'codex', 'claude-code', 'opencode', 'api', 'lmstudio'}
            if arg not in allowed:
                emit({'type': 'error', 'error': f'Unknown provider: {arg}. Options: {", ".join(sorted(allowed))}', 'request_id': request_id})
                return
            current_provider = self._current_provider_name()
            if not skip_prompt and current_provider and current_provider != arg and self._has_transferable_context():
                self._prompt_provider_switch(arg, request_id, source='setup-provider')
                return

            use_session_override = bool(self._active_agent_id)
            target_state = dict(session_override) if use_session_override else dict(onboarding)
            target_state['provider_mode'] = 'provider'
            target_state['provider'] = arg
            target_state['complete'] = False

            def _persist_target_state() -> None:
                onboarding.update({
                    'provider_mode': target_state.get('provider_mode', onboarding.get('provider_mode', '')),
                    'provider': target_state.get('provider', onboarding.get('provider', '')),
                    'provider_auth': target_state.get('provider_auth', onboarding.get('provider_auth', '')),
                    'model': target_state.get('model', onboarding.get('model', '')),
                    'provider_model': target_state.get('provider_model', onboarding.get('provider_model', '')),
                    'project': target_state.get('project', onboarding.get('project', '')),
                    'complete': target_state.get('complete', onboarding.get('complete', False)),
                    'step': target_state.get('step', onboarding.get('step', 'provider-mode')),
                })
                self._save_onboarding(onboarding)
                if use_session_override and self._active_agent_id:
                    save_session_provider_config(STATE_DIR, self._active_agent_id, target_state)

            def _revert_target_state() -> None:
                if use_session_override and self._active_agent_id:
                    if session_override:
                        save_session_provider_config(STATE_DIR, self._active_agent_id, session_override)
                    else:
                        from provider_bridge import clear_session_provider_config
                        clear_session_provider_config(STATE_DIR, self._active_agent_id)
                else:
                    self._save_onboarding(onboarding)

            # For OAuth providers, try to find existing credentials first
            if arg in ('claude-code', 'codex'):
                target_state['step'] = 'provider-auth'
                _persist_target_state()
                provider_map = {'claude-code': 'anthropic', 'codex': 'openai-codex'}
                provider_id = provider_map[arg]

                # Try to find existing Claude credentials
                # First check our own auth store, then Claude's credentials file
                # Use /setup provider claude-code --force to skip and do fresh OAuth
                existing_token = None
                if arg == 'claude-code' and not force_oauth:
                    # Check Charon's own auth store first
                    existing_token = self._find_charon_auth_token('anthropic')
                    # Then try Claude Code's credentials file
                    if not existing_token:
                        existing_token = self._find_claude_credentials()
                    if existing_token:
                        # Save existing token to charon auth store
                        try:
                            import charon_auth
                            store = charon_auth._load_auth()
                            store['active_provider'] = 'anthropic'
                            store.setdefault('providers', {})
                            store['providers']['anthropic'] = {
                                'tokens': {'access_token': existing_token},
                                'last_login': charon_auth._now(),
                                'auth_type': 'existing_claude',
                            }
                            charon_auth._save_auth(store)

                            target_state['provider_auth'] = 'existing'
                            target_state['step'] = 'model'
                            _persist_target_state()

                            emit({'type': 'status', 'message': '✓ Found existing Claude credentials! Token imported.', 'request_id': request_id})
                            # Auto-trigger model picker
                            self._run_setup_command('model', request_id)
                            return
                        except Exception as e:
                            emit({'type': 'status', 'message': f'Found credentials but import failed: {e}. Falling back to OAuth.', 'request_id': request_id})

                # Run OAuth with local callback server in a background thread
                try:
                    import charon_auth
                    import threading

                    emit({'type': 'status', 'message': f'Setting up OAuth for {arg}...', 'request_id': request_id})

                    def _run_oauth():
                        try:
                            def _status(msg: str):
                                if msg.startswith('AUTH_URL::'):
                                    url = msg.split('AUTH_URL::', 1)[1].strip()
                                    emit({'type': 'auth_url', 'url': url, 'provider': arg, 'request_id': request_id})
                                elif msg.startswith('AUTH_INFO::'):
                                    emit({'type': 'status', 'message': msg.split('AUTH_INFO::', 1)[1].strip(), 'request_id': request_id})
                                else:
                                    emit({'type': 'status', 'message': msg, 'request_id': request_id})

                            token_data = charon_auth.login_oauth(provider_id, status_cb=_status)

                            target_state['provider_auth'] = 'oauth'
                            target_state['step'] = 'model'
                            _persist_target_state()

                            emit({'type': 'status', 'message': f'✓ Authentication successful!', 'request_id': request_id})
                            # Auto-trigger model picker
                            self._run_setup_command('model', request_id)
                        except Exception as e:
                            _revert_target_state()
                            emit({'type': 'error', 'error': f'Auth failed: {e}', 'request_id': request_id})
                            emit({'type': 'status', 'message': 'Provider switch interrupted. Restored previous provider for this session.', 'request_id': request_id})
                            emit({'type': 'status', 'message': 'You can also try: /setup api-key <your-key>', 'request_id': request_id})

                    t = threading.Thread(target=_run_oauth, daemon=True)
                    t.start()
                    emit({'type': 'status', 'message': f'Starting {arg} authentication... Opening browser.', 'request_id': request_id})
                except Exception as e:
                    _revert_target_state()
                    emit({'type': 'error', 'error': f'Auth setup failed: {e}', 'request_id': request_id})
                    emit({'type': 'status', 'message': 'Provider switch interrupted. Restored previous provider for this session.', 'request_id': request_id})
            else:
                if arg == 'lmstudio':
                    detected = self._detect_lmstudio_models()
                    current_model = str(target_state.get('model') or target_state.get('provider_model') or '').strip()
                    chosen_model = ''
                    if current_model and current_model in detected:
                        chosen_model = current_model
                    elif detected:
                        chosen_model = detected[0]
                    else:
                        chosen_model = os.environ.get('CHARON_LOCAL_MODEL', '').strip() or 'qwen3-30b-a3b'

                    target_state['model'] = chosen_model
                    target_state['provider_model'] = chosen_model
                    target_state['complete'] = True
                    target_state['step'] = 'done'
                    _persist_target_state()
                    self.engine = None
                    if detected:
                        emit({'type': 'status', 'message': f'Provider set to {arg}. Auto-selected model {chosen_model}.', 'request_id': request_id})
                    else:
                        emit({'type': 'status', 'message': f'Provider set to {arg}. No local model list detected, using {chosen_model}.', 'request_id': request_id})
                    effective_onboarding = dict(onboarding)
                    effective_onboarding.update(target_state)
                    self._on_setup_complete(effective_onboarding, request_id)
                else:
                    target_state['step'] = 'model'
                    _persist_target_state()
                    emit({'type': 'status', 'message': f'Provider set to {arg}. Now run /setup model <model_name>', 'request_id': request_id})
        elif subcmd == 'model':
            provider_state = dict(onboarding)
            if session_override:
                provider_state.update(session_override)
            provider = str(provider_state.get('provider') or '').strip()
            # Known models per provider
            known_models = {
                'claude-code': [
                    # 4.6 (latest)
                    {'id': 'claude-sonnet-4-6', 'desc': 'Sonnet 4.6 — latest, fast'},
                    {'id': 'claude-opus-4-6', 'desc': 'Opus 4.6 — latest, most capable'},
                    # 4.5
                    {'id': 'claude-sonnet-4-5', 'desc': 'Sonnet 4.5'},
                    {'id': 'claude-opus-4-5', 'desc': 'Opus 4.5'},
                    # 4.1
                    {'id': 'claude-opus-4-1', 'desc': 'Opus 4.1'},
                    # 4.0
                    {'id': 'claude-sonnet-4-20250514', 'desc': 'Sonnet 4.0'},
                    {'id': 'claude-opus-4-20250514', 'desc': 'Opus 4.0'},
                    # Haiku
                    {'id': 'claude-haiku-4-5', 'desc': 'Haiku 4.5 — fastest'},
                    # 3.x
                    {'id': 'claude-3-7-sonnet-20250219', 'desc': 'Sonnet 3.7'},
                    {'id': 'claude-3-5-sonnet-20241022', 'desc': 'Sonnet 3.5 v2'},
                    {'id': 'claude-3-5-haiku-20241022', 'desc': 'Haiku 3.5'},
                ],
                'codex': [
                    {'id': 'gpt-5.4', 'desc': 'GPT 5.4 — most capable (recommended)'},
                    {'id': 'gpt-5', 'desc': 'GPT 5'},
                    # Note: o3, o4-mini, gpt-4.1, gpt-4o, codex-mini etc. are NOT supported
                    # with Codex OAuth (ChatGPT subscription). Only gpt-5 family works.
                ],
                'lmstudio': [],  # dynamic — detected from LM Studio
                'api': [],
                'opencode': [],
            }

            # For local providers, try to detect available models
            if provider == 'lmstudio' and not known_models['lmstudio']:
                try:
                    import httpx
                    resp = httpx.get('http://127.0.0.1:1234/v1/models', timeout=3.0)
                    if resp.status_code == 200:
                        for m in resp.json().get('data', []):
                            mid = m.get('id', '')
                            if mid:
                                known_models['lmstudio'].append({'id': mid, 'desc': 'Local model'})
                except Exception:
                    pass
            models = known_models.get(provider, [])

            if not arg:
                # No model specified — show model picker
                if models:
                    emit({
                        'type': 'model_picker',
                        'models': models,
                        'provider': provider,
                        'request_id': request_id,
                    })
                else:
                    emit({'type': 'error', 'error': 'Usage: /setup model <model_name>', 'request_id': request_id})
                return

            # Validate model name — warn but don't reject custom names
            model_ids = [m['id'] for m in models]
            if models and arg not in model_ids:
                close = [m for m in model_ids if arg.lower() in m.lower()]
                if close:
                    emit({'type': 'status', 'message': f'Model "{arg}" not in known list. Close matches: {", ".join(close)}', 'request_id': request_id})
                else:
                    emit({'type': 'status', 'message': f'Model "{arg}" not in known list. Using it anyway.', 'request_id': request_id})

            target_state = dict(session_override) if session_override else dict(onboarding)
            target_state['model'] = arg
            target_state['provider_model'] = arg

            # Auto-complete if project is already set (from previous setup or default)
            project = str(target_state.get('project') or onboarding.get('project') or '').strip()
            if not project:
                project = str(ROOT)
                target_state['project'] = project

            target_state['complete'] = True
            target_state['step'] = 'done'
            onboarding.update(target_state)
            self._save_onboarding(onboarding)
            if session_override and self._active_agent_id:
                save_session_provider_config(STATE_DIR, self._active_agent_id, target_state)
            self.engine = None
            emit({'type': 'status', 'message': f'✓ Model set to {arg}. Setup complete.', 'request_id': request_id})
            effective_onboarding = dict(onboarding)
            effective_onboarding.update(target_state)
            self._on_setup_complete(effective_onboarding, request_id)
        elif subcmd == 'shade-provider':
            if not arg:
                # Show shade provider picker
                emit({
                    'type': 'shade_provider_picker',
                    'options': [
                        {'id': 'same', 'desc': 'Same as main provider (default)'},
                        {'id': 'lmstudio', 'desc': 'LM Studio (local)'},
                        {'id': 'api', 'desc': 'OpenAI-compatible API'},
                    ],
                    'request_id': request_id,
                })
                return
            if arg in ('same', 'skip'):
                from model_registry import load_registry, save_registry
                reg = load_registry(STATE_DIR)
                reg['shade_model_mode'] = 'same'
                save_registry(STATE_DIR, reg)
                onboarding['step'] = 'complete'
                self._save_onboarding(onboarding)
                emit({'type': 'status', 'message': '✓ Shade will use same provider as main agent.', 'request_id': request_id})
                self._run_setup_command('complete', request_id)
            elif arg == 'api':
                onboarding['shade_provider'] = arg
                onboarding['step'] = 'shade-url'
                self._save_onboarding(onboarding)
                emit({'type': 'status', 'message': f'Shade provider set to {arg}. Now provide the base URL: /setup shade-url <url>', 'request_id': request_id})
            else:
                onboarding['shade_provider'] = arg
                onboarding['step'] = 'shade-model'
                self._save_onboarding(onboarding)
                emit({'type': 'status', 'message': f'Shade provider set to {arg}. Now pick a model: /setup shade-model <model_name>', 'request_id': request_id})
                self._run_setup_command('shade-model', request_id)
        elif subcmd == 'shade-url':
            if not arg:
                emit({'type': 'error', 'error': 'Usage: /setup shade-url <url>', 'request_id': request_id})
                return
            onboarding['shade_base_url'] = arg
            onboarding['step'] = 'shade-model'
            self._save_onboarding(onboarding)
            emit({'type': 'status', 'message': f'Shade base URL set to {arg}. Now pick a model: /setup shade-model <model_name>', 'request_id': request_id})
            self._run_setup_command('shade-model', request_id)
        elif subcmd == 'shade-model':
            shade_provider = str(onboarding.get('shade_provider') or '').strip()
            if not arg:
                # Show model picker
                if shade_provider == 'lmstudio':
                    try:
                        import httpx
                        resp = httpx.get('http://127.0.0.1:1234/v1/models', timeout=3.0)
                        if resp.status_code == 200:
                            models = [{'id': m.get('id', ''), 'desc': 'Local model'} for m in resp.json().get('data', []) if m.get('id')]
                            if models:
                                emit({'type': 'model_picker', 'models': models, 'provider': shade_provider, 'context': 'shade', 'request_id': request_id})
                                return
                    except Exception:
                        pass
                emit({'type': 'error', 'error': 'Usage: /setup shade-model <model_name>', 'request_id': request_id})
                return
            # Save model to registry
            from model_registry import load_registry, save_registry
            reg = load_registry(STATE_DIR)
            reg['shade_model_mode'] = 'fixed'
            # Parse provider/model format (e.g., lmstudio/qwen3-30b)
            if '/' in arg:
                parts = arg.split('/', 1)
                reg['shade_provider'] = parts[0]
                reg['shade_model'] = parts[1]
            else:
                reg['shade_model'] = arg
                reg['shade_provider'] = shade_provider or 'openai'
            if reg.get('shade_provider') in ('lmstudio', 'local', 'ollama'):
                reg['shade_base_url'] = 'http://127.0.0.1:1234/v1'
                reg['shade_api_key'] = 'not-needed'
            elif shade_provider == 'api':
                shade_base_url = str(onboarding.get('shade_base_url') or '').strip()
                if shade_base_url:
                    reg['shade_base_url'] = shade_base_url
            save_registry(STATE_DIR, reg)
            # Don't touch onboarding — shade config is independent
            emit({'type': 'status', 'message': f'✓ Shade model set to {arg} (provider: {reg.get("shade_provider", "auto")})', 'request_id': request_id})
            self._run_setup_command('complete', request_id)
        elif subcmd == 'project':
            onboarding['project'] = arg or str(ROOT)
            onboarding['step'] = 'complete'
            self._save_onboarding(onboarding)
            emit({'type': 'status', 'message': f'Project set to {arg or str(ROOT)}.', 'request_id': request_id})
        elif subcmd == 'auth-code':
            if not arg:
                emit({'type': 'error', 'error': 'Paste the authorization code: /setup auth-code <CODE>', 'request_id': request_id})
                return
            if not hasattr(self, '_pending_auth') or not self._pending_auth:
                emit({'type': 'error', 'error': 'No pending auth. Run /setup provider claude-code first.', 'request_id': request_id})
                return
            try:
                import charon_auth
                import urllib.parse

                pa = self._pending_auth
                provider = charon_auth.PROVIDERS[pa['provider_id']]

                # Parse the code (might be a full URL or just the code)
                code = arg.strip()
                if '?' in code:
                    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)
                    code = parsed.get('code', [code])[0]
                if '#' in code:
                    parts = code.split('#', 1)
                    code = parts[0]

                emit({'type': 'status', 'message': 'Exchanging code for tokens...', 'request_id': request_id})

                token_data = charon_auth._exchange_code_json(
                    provider, code, pa['verifier'], state=pa['state'],
                )

                # Save auth
                store = charon_auth._load_auth()
                store['active_provider'] = provider.id
                store.setdefault('providers', {})
                store['providers'][provider.id] = {
                    'tokens': token_data,
                    'last_login': charon_auth._now(),
                    'auth_type': 'oauth',
                }
                charon_auth._save_auth(store)

                onboarding['provider_auth'] = 'oauth'
                onboarding['step'] = 'model'
                self._save_onboarding(onboarding)
                self._pending_auth = None
                self.engine = None

                emit({'type': 'status', 'message': '✓ Authentication successful! Now run /setup model <model_name>', 'request_id': request_id})
            except Exception as e:
                emit({'type': 'error', 'error': f'Token exchange failed: {e}', 'request_id': request_id})
                emit({'type': 'status', 'message': 'You can also set an API key directly: /setup api-key <key>', 'request_id': request_id})
        elif subcmd == 'complete':
            onboarding['complete'] = True
            onboarding['step'] = 'done'
            self._save_onboarding(onboarding)
            self.engine = None  # force re-creation with new config
            self._on_setup_complete(onboarding, request_id)
        elif subcmd in ('api-key',):
            onboarding['api_key'] = arg
            if not onboarding.get('provider'):
                onboarding['provider'] = 'api'
            onboarding['provider_mode'] = 'provider'
            self._save_onboarding(onboarding)
            self.engine = None
            emit({'type': 'status', 'message': 'API key saved.', 'request_id': request_id})
        elif subcmd == 'no-provider':
            onboarding['provider_mode'] = 'no-provider'
            onboarding['provider'] = ''
            onboarding['complete'] = True
            onboarding['step'] = 'done'
            self._save_onboarding(onboarding)
            self.engine = None
            self._on_setup_complete(onboarding, request_id)
        else:
            emit({'type': 'error', 'error': f'Unknown setup command: {subcmd}', 'request_id': request_id})

    def _repair_incomplete_onboarding_startup(self) -> dict:
        onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
        if not isinstance(onboarding, dict):
            return {}
        if onboarding.get('complete') or str(onboarding.get('provider_mode') or '').strip().lower() != 'provider':
            return onboarding

        auth_store = _load_json(STATE_DIR / 'auth' / 'auth.json', {})
        providers = auth_store.get('providers', {}) if isinstance(auth_store, dict) else {}
        model = str(onboarding.get('model') or onboarding.get('provider_model') or '').strip()
        provider = str(onboarding.get('provider') or '').strip().lower()

        repaired_provider = ''
        repaired_auth = str(onboarding.get('provider_auth') or '').strip()
        if (provider == 'claude-code' or model.startswith('claude-')) and self._find_charon_auth_token('anthropic'):
            repaired_provider = 'claude-code'
            repaired_auth = repaired_auth or 'existing'
        elif (provider == 'codex' or model.startswith('gpt-') or model in ('o3', 'o4-mini', 'o3-mini', 'codex-mini-latest')) and self._find_charon_auth_token('openai-codex'):
            repaired_provider = 'codex'
            repaired_auth = repaired_auth or 'oauth'
        elif provider in ('lmstudio', 'local', 'ollama') and model and not model.startswith('claude-') and not model.startswith('gpt-') and model not in ('o3', 'o4-mini', 'o3-mini', 'codex-mini-latest'):
            repaired_provider = 'lmstudio'
            repaired_auth = repaired_auth or 'local'

        if not repaired_provider:
            return onboarding

        repaired = dict(onboarding)
        repaired['provider'] = repaired_provider
        repaired['provider_mode'] = 'provider'
        repaired['provider_auth'] = repaired_auth
        repaired['complete'] = True
        repaired['step'] = 'done'
        if model:
            repaired['model'] = model
            repaired['provider_model'] = model
        self._save_onboarding(repaired)
        emit({'type': 'status', 'message': f'Restored provider config: {repaired_provider}/{model or "(default model)"}'})
        return repaired

    def _find_charon_auth_token(self, provider_id: str) -> str | None:
        """Check Charon's own auth store for a valid token."""
        auth_file = STATE_DIR / 'auth' / 'auth.json'
        if not auth_file.exists():
            return None
        try:
            store = json.loads(auth_file.read_text())
            provider_auth = store.get('providers', {}).get(provider_id, {})
            tokens = provider_auth.get('tokens', {})
            access_token = tokens.get('access_token', '').strip()
            if access_token:
                return access_token
        except Exception:
            pass
        return None

    def _find_claude_credentials(self) -> str | None:
        """Look for existing Claude Code credentials on this machine.
        Auto-refreshes expired tokens using the refresh token.
        """
        import os, time
        cred_path = os.path.expanduser('~/.claude/.credentials.json')
        if not os.path.exists(cred_path):
            return None
        try:
            data = json.loads(open(cred_path).read())
            oauth = data.get('claudeAiOauth', {})
            token = oauth.get('accessToken', '')
            refresh_token = oauth.get('refreshToken', '')
            expires_at = oauth.get('expiresAt', 0)

            if not token or not token.startswith('sk-ant-'):
                return None

            # Check if token is expired
            now_ms = time.time() * 1000
            if expires_at and expires_at < now_ms and refresh_token:
                # Token expired — try to refresh
                refreshed = self._refresh_anthropic_token(refresh_token)
                if refreshed:
                    # Update the credentials file
                    oauth['accessToken'] = refreshed['access_token']
                    if refreshed.get('refresh_token'):
                        oauth['refreshToken'] = refreshed['refresh_token']
                    oauth['expiresAt'] = int(time.time() * 1000) + refreshed.get('expires_in', 3600) * 1000
                    data['claudeAiOauth'] = oauth
                    try:
                        with open(cred_path, 'w') as f:
                            json.dump(data, f)
                        os.chmod(cred_path, 0o600)
                    except Exception:
                        pass
                    return refreshed['access_token']
                return None  # refresh failed

            return token
        except Exception:
            pass
        return None

    def _refresh_anthropic_token(self, refresh_token: str) -> dict | None:
        """Refresh an expired Anthropic OAuth token."""
        try:
            import httpx
            resp = httpx.post(
                'https://platform.claude.com/v1/oauth/token',
                json={
                    'grant_type': 'refresh_token',
                    'client_id': '9d1c250a-e61b-44d9-88ed-5944d1962f5e',
                    'refresh_token': refresh_token,
                },
                headers={'Accept': 'application/json'},
                timeout=30.0,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    def _save_onboarding(self, state: dict):
        from datetime import datetime, timezone
        state['updated_at'] = datetime.now(timezone.utc).isoformat()
        path = STATE_DIR / 'onboarding.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        tmp.replace(path)

    def _on_setup_complete(self, onboarding: dict, request_id: str | None):
        """Post-setup: create default agent, detect other agents, report results.

        Mirrors pi-agent behavior: once configured, you're immediately ready to go.
        """
        provider_mode = str(onboarding.get('provider_mode') or '').lower()
        provider = str(onboarding.get('provider') or '').lower()
        project = str(onboarding.get('project') or str(ROOT)).strip()
        model = str(onboarding.get('model') or onboarding.get('provider_model') or '').strip()

        results = []

        # 1. Create default agent (unless no-provider mode or agent already exists)
        agent_created = None
        if provider_mode != 'no-provider':
            try:
                from agent_lifecycle import list_agents, create_agent
                existing = list_agents()
                has_charon = any(
                    a.get('role') == 'charon' and a.get('status') != 'stopped'
                    for a in existing
                )
                if not has_charon:
                    agent_created = create_agent(
                        name='',  # auto-name: charon-<project>-01
                        mode='persistent',
                        goal=f'Primary agent for {project.split("/")[-1] or "project"}',
                        project=project,
                        role='charon',
                        visibility='user',
                        require_tmux=False,  # don't require tmux for auto-created agent
                    )
                    results.append(f'Created agent {agent_created["name"]} ({agent_created["id"]})')
                else:
                    results.append(f'Agent already exists ({len(existing)} agents)')
            except Exception as e:
                results.append(f'Agent creation failed: {e}')
        else:
            results.append('No-provider mode — skipped agent creation')

        # 2. Detect running agent processes
        detected = []
        try:
            sys.path.insert(0, str(ROOT / 'apps' / 'tui'))
            from process_inspector import detect_agent_processes, summarize_agent_processes
            procs = detect_agent_processes()
            if procs:
                detected = summarize_agent_processes(procs)
                results.append(f'Detected {len(procs)} running agent process(es)')
            else:
                results.append('No other agent processes detected')
        except Exception as e:
            results.append(f'Process detection failed: {e}')

        # 3. Sync to SQLite store
        try:
            from store_adapter import get_db, onboarding_set as db_onboarding_set
            db = get_db(STATE_DIR)
            db_onboarding_set(db, onboarding)
        except Exception:
            pass

        # 4. Emit setup complete event
        emit({
            'type': 'setup_complete',
            'provider': provider,
            'model': model,
            'agent': agent_created.get('name') if agent_created else None,
            'request_id': request_id,
        })

        # 5. Refresh the UI so dashboard shows the new agent
        self.handle_refresh(request_id)

    def handle_chat(self, message: str, request_id: str | None):
        """Handle a chat message — run through conversation engine with streaming."""
        stripped = message.strip()
        if self._pending_provider_switch and stripped in {'1', '2'}:
            self.handle_command(f'/{stripped}', request_id)
            return
        if message.startswith('/'):
            self.handle_command(message, request_id)
            return

        # Natural-language Libris trigger
        libris_match = re.match(r'^(?:start|run|launch)\s+(?:a\s+)?libris\s+(?:research\s+project|research|project)?\s*(?:on|for)?\s+(.+)$', stripped, re.I)
        if libris_match:
            topic_prompt = libris_match.group(1).strip()
            self.handle_command(f'/libris {topic_prompt}', request_id)
            return

        # Natural-language software-dev trigger
        devop_match = re.match(
            r'^(?:start|run|launch|begin|kick\s+off|create)\s+(?:an?\s+)?(?:autonomous\s+)?(?:software\s+(?:development|dev)|software|dev|coding)\s+(?:project|operation|build)?\s*(?:that|to|for)?\s+(.+)$',
            stripped,
            re.I,
        )
        if devop_match:
            build_prompt = devop_match.group(1).strip()
            self.handle_command(f'/devop {build_prompt}', request_id)
            return

        cron_nl = _natural_language_to_cron(stripped)
        cron_url_match = re.search(r'check\s+(https?://\S+)', stripped, re.I)
        if cron_nl and cron_url_match:
            url = cron_url_match.group(1).rstrip('.,)')
            self.handle_command(f'/automate cron "{cron_nl}" check {url}', request_id)
            return

        monitor_match = re.match(r'^(?:every\s+.+?|hourly|daily)\s+check\s+(https?://\S+)(?:\s+and\s+report\s+if\s+it\s+(?:isn\'t|is\s+not|fails?|breaks?))?$', stripped, re.I)
        if monitor_match:
            interval_phrase = stripped[:monitor_match.start(1)].strip()
            url = monitor_match.group(1).rstrip('.,)')
            self.handle_command(f'/monitor {interval_phrase} {url}', request_id)
            return

        continuous_match = re.match(r'^(?:continuously|nonstop|always on|all day)\s+check\s+(https?://\S+)(?:.*)?$', stripped, re.I)
        if continuous_match:
            url = continuous_match.group(1).rstrip('.,)')
            self.handle_command(f'/automate continuous check {url}', request_id)
            return

        route = self._match_nl_orchestration_command(stripped)
        if route:
            command, status = route
            source = 'shades-parser' if 'via shades parser' in status.lower() else ('fast-path' if 'via fast-path' in status.lower() else 'unknown')
            self._last_orchestration_parse = {
                'source': source,
                'status': status,
                'command': command,
                'input': stripped[:240],
                'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            }
            emit({'type': 'status', 'message': status, 'request_id': request_id})
            emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
            self.handle_command(command, request_id)
            return

        engine, error = self._ensure_engine()
        if not engine:
            emit({'type': 'error', 'error': error, 'request_id': request_id})
            return
        # _ensure_engine may assign a fresh session id; persist any in-memory
        # outcome state now that we have one.
        self._save_session_outcomes()

        # Session outcome ledger: resolve the previous active task based on
        # how the user is steering now, then start a new active task if this
        # message is a concrete request.
        if self._session_tasks:
            if self._is_ack_message(message) or self._starts_with_ack(message):
                self._resolve_pending_outcome('completed')
            elif self._is_redirect_message(message):
                self._resolve_pending_outcome('failed')
            elif self._parse_intent(message):
                self._resolve_pending_outcome('completed')
        self._start_outcome_for_message(message)
        self._improve_active_outcome_title_background(message, request_id)
        emit({
            'type': 'refresh',
            'payload': {'session_info': self._get_session_info()},
            'request_id': request_id,
        })

        self.chat_history.append({'role': 'user', 'content': message})

        async def _run():
            text_parts = []
            thinking_started = False
            _tool_calls_record = []
            _total_input_tokens = 0
            _total_output_tokens = 0
            _total_turns = 0
            try:
                async for event in engine.submit(message):
                    if event.type == 'thinking_delta':
                        if not thinking_started:
                            emit({'type': 'thinking_start', 'request_id': request_id})
                            thinking_started = True
                        if self.visible_thoughts and self._thoughts_supported():
                            emit({
                                'type': 'thinking_delta',
                                'text': event.data.get('text', ''),
                                'request_id': request_id,
                            })
                    elif event.type == 'text_delta':
                        text = event.data.get('text', '')
                        text_parts.append(text)
                        emit({'type': 'chat_delta', 'text': text, 'request_id': request_id})
                    elif event.type == 'tool_call':
                        _tool_calls_record.append({
                            'tool': event.data.get('tool_name', ''),
                            'arguments': event.data.get('arguments', {}),
                        })
                        emit({
                            'type': 'tool_call',
                            'tool_name': event.data.get('tool_name', ''),
                            'arguments': event.data.get('arguments', {}),
                            'tool_call_id': event.data.get('tool_call_id', ''),
                            'request_id': request_id,
                        })
                    elif event.type == 'tool_execution_output':
                        emit({
                            'type': 'tool_result_delta',
                            'tool_name': event.data.get('tool_name', ''),
                            'content': event.data.get('content', ''),
                            'chunk': event.data.get('chunk', ''),
                            'tool_call_id': event.data.get('tool_call_id', ''),
                            'request_id': request_id,
                        })
                    elif event.type == 'tool_execution_end':
                        # Update the last tool call record with result
                        if _tool_calls_record:
                            _tool_calls_record[-1]['result'] = event.data.get('content', '')[:500]
                            _tool_calls_record[-1]['is_error'] = event.data.get('is_error', False)
                        emit({
                            'type': 'tool_result',
                            'tool_name': event.data.get('tool_name', ''),
                            'content': event.data.get('content', ''),
                            'is_error': event.data.get('is_error', False),
                            'truncated': event.data.get('truncated', False),
                            'tool_call_id': event.data.get('tool_call_id', ''),
                            'request_id': request_id,
                        })
                    elif event.type == 'turn_end':
                        _total_turns += 1
                    elif event.type == 'message_end':
                        usage = event.data.get('usage', {})
                        input_tokens = int(usage.get('input_tokens', 0) or 0)
                        output_tokens = int(usage.get('output_tokens', 0) or 0)

                        # Fallback for providers like LM Studio that may not emit usage
                        # in streamed responses: estimate from current context + response.
                        if self.engine and input_tokens <= 0:
                            try:
                                input_tokens = sum(
                                    len((getattr(m, 'content', '') or '')) // 4
                                    for m in self.engine.messages[:-1]
                                )
                            except Exception:
                                input_tokens = 0
                        if output_tokens <= 0:
                            output_tokens = len((event.data.get('content', '') or '')) // 4

                        _total_input_tokens += input_tokens
                        _total_output_tokens += output_tokens

                        # Context = input + output tokens from this request
                        # (input_tokens = entire context sent to the API)
                        context_tokens = input_tokens + output_tokens
                        context_pct = 0
                        context_window = 200000

                        if self.engine:
                            try:
                                context_window = int(getattr(self.engine.model, 'context_window', 200000) or 200000)
                            except Exception:
                                context_window = 200000

                        if context_tokens > 0:
                            context_pct = min(100, int(context_tokens * 100 / max(1, context_window)))
                        elif self.engine:
                            # Fallback: estimate from message content
                            msg_tokens = sum(
                                len(getattr(m, 'content', '') or '') // 4
                                for m in self.engine.messages
                            )
                            context_pct = min(100, int(msg_tokens * 100 / max(1, context_window)))
                            context_tokens = msg_tokens

                        emit({
                            'type': 'usage',
                            'input_tokens': input_tokens,
                            'output_tokens': output_tokens,
                            'context_tokens': context_tokens,
                            'context_pct': context_pct,
                            'context_window': context_window,
                            'request_id': request_id,
                        })
                    elif event.type == 'turn_end':
                        emit({
                            'type': 'turn_complete',
                            'stop_reason': event.data.get('stop_reason', ''),
                            'turn': event.data.get('turn', 0),
                            'request_id': request_id,
                        })
                    elif event.type == 'error':
                        emit({
                            'type': 'error',
                            'error': event.data.get('error', 'unknown error'),
                            'request_id': request_id,
                        })
                    elif event.type == 'steer_delivered':
                        emit({
                            'type': 'steer_delivered',
                            'content': event.data.get('content', ''),
                            'skipped_tools': event.data.get('skipped_tools', 0),
                            'request_id': request_id,
                        })
                    elif event.type == 'follow_up_delivered':
                        emit({
                            'type': 'follow_up_delivered',
                            'content': event.data.get('content', ''),
                            'request_id': request_id,
                        })
                    elif event.type == 'retry':
                        attempt = event.data.get('attempt', 1)
                        wait = event.data.get('wait_seconds', 3)
                        emit({'type': 'status', 'message': f'⟳ Retrying (attempt {attempt}/2, waiting {wait}s)...', 'request_id': request_id})
                    elif event.type == 'compaction_start':
                        emit({'type': 'status', 'message': 'Compacting context...', 'request_id': request_id})
                    elif event.type == 'compaction_end':
                        emit({'type': 'status', 'message': 'Context compacted.', 'request_id': request_id})

            except Exception as e:
                emit({'type': 'error', 'error': str(e), 'request_id': request_id})

            full_text = ''.join(text_parts)
            self.chat_history.append({'role': 'assistant', 'content': full_text})

            # Record task in working memory + task queue (zero LLM cost)
            if self._active_agent_id and engine:
                try:
                    from task_summarizer import summarize_fast
                    from agent_runtime import update_working_memory
                    from execution_memory import create_task_episode
                    import uuid as _uuid

                    task_id = f'chat-{_uuid.uuid4().hex[:8]}'
                    # Get the user message that triggered this
                    user_msg = message[:200] if message else ''

                    summary = summarize_fast(
                        instruction=user_msg,
                        tool_calls=_tool_calls_record,
                        response_text=full_text,
                        errors=[],
                        total_turns=_total_turns,
                    )

                    # Only write to persistent agent memory when this session
                    # was explicitly bound to a persistent agent. Fresh sessions
                    # should not silently share working memory.
                    memory_agent_id = getattr(self, '_bound_agent_id', None)
                    if memory_agent_id:
                        update_working_memory(
                            STATE_DIR, memory_agent_id,
                            task_id=task_id, summary=summary,
                        )

                    # Update the session outcome ledger entry for this task.
                    if not hasattr(self, '_session_tasks'):
                        self._session_tasks = []
                    files_touched = []
                    try:
                        for tc in _tool_calls_record:
                            args = tc.get('arguments', {}) or {}
                            for key in ('path', 'oldText', 'newText'):
                                val = args.get(key)
                                if key == 'path' and isinstance(val, str) and val and val not in files_touched:
                                    files_touched.append(val)
                    except Exception:
                        files_touched = []

                    # Determine if the agent concluded the task or is mid-flight
                    # (e.g. asking a clarifying question). We consider a task done
                    # if it made at least one tool call (did real work) OR if the
                    # response text doesn't look like a question/clarification.
                    agent_concluded = (
                        len(_tool_calls_record) > 0
                        or not self._is_question_message(full_text.strip())
                    )
                    new_status = 'completed' if agent_concluded else 'active'

                    updated = False
                    for item in reversed(self._session_tasks):
                        if item.get('status') == 'active':
                            item['status'] = new_status
                            if new_status == 'completed':
                                item['resolved_at'] = time.time()
                            item['summary'] = summary
                            item['detail'] = (
                                f'Task: {user_msg[:100]}\n'
                                f'Outcome: {item.get("title", "")}\n'
                                f'Result: {summary}\n'
                                f'Tools: {len(_tool_calls_record)} calls, {_total_turns} turns\n'
                                f'Tokens: {_total_input_tokens}↑ {_total_output_tokens}↓'
                            )
                            item['tokens_in'] = _total_input_tokens
                            item['tokens_out'] = _total_output_tokens
                            item['tool_calls'] = len(_tool_calls_record)
                            item['turns'] = _total_turns
                            item['files_touched'] = files_touched
                            item['ts'] = time.time()
                            updated = True
                            break
                    if not updated and self._parse_intent(user_msg):
                        self._start_outcome_for_message(user_msg)
                        if self._session_tasks:
                            self._session_tasks[-1]['status'] = new_status
                            if new_status == 'completed':
                                self._session_tasks[-1]['resolved_at'] = time.time()
                            self._session_tasks[-1]['summary'] = summary
                            self._session_tasks[-1]['detail'] = f'Task: {user_msg[:100]}\nResult: {summary}'
                            self._session_tasks[-1]['tokens_in'] = _total_input_tokens
                            self._session_tasks[-1]['tokens_out'] = _total_output_tokens
                            self._session_tasks[-1]['tool_calls'] = len(_tool_calls_record)
                            self._session_tasks[-1]['turns'] = _total_turns
                            self._session_tasks[-1]['files_touched'] = files_touched
                    self._save_session_outcomes()
                    emit({
                        'type': 'refresh',
                        'payload': {'session_info': self._get_session_info()},
                        'request_id': request_id,
                    })

                    try:
                        create_task_episode(
                            STATE_DIR,
                            session_id=self._active_agent_id,
                            agent_id=memory_agent_id or '',
                            project_root=str(engine.project_root),
                            provider=str(self._current_provider_name() or getattr(engine, 'provider_name', 'unknown')),
                            objective=user_msg,
                            summary=summary,
                            tool_calls=_tool_calls_record,
                            response_text=full_text,
                            total_turns=_total_turns,
                            input_tokens=_total_input_tokens,
                            output_tokens=_total_output_tokens,
                        )
                    except Exception:
                        pass
                except Exception:
                    pass

            # Persist conversation
            if self._active_agent_id and engine:
                try:
                    # When lossless store is active, messages are already persisted
                    # to SQLite on every turn.  Write JSONL as backup using the
                    # FULL history from the store (not engine.messages which may
                    # have been truncated by legacy compaction).
                    from conversation_store import save_conversation, message_to_dict
                    msgs_to_save = None
                    if engine.has_lossless_store:
                        msgs_to_save = _full_messages_from_store(self._active_agent_id)
                    if msgs_to_save is None:
                        msgs_to_save = list(engine.messages)
                    save_conversation(STATE_DIR, self._active_agent_id,
                        [message_to_dict(m) for m in msgs_to_save])
                    # Register session on first save (not on startup)
                    if not hasattr(self, '_session_registered'):
                        self._session_registered = True
                        try:
                            from session_registry import register_session
                            register_session(STATE_DIR, self._active_agent_id)
                        except Exception:
                            pass
                except Exception:
                    pass

            emit({
                'type': 'chat_complete',
                'summary': full_text[:200],
                'request_id': request_id,
            })

            # Write to persistent agent inbox only for explicitly bound agents.
            try:
                bound_agent_id = getattr(self, '_bound_agent_id', None)
                if bound_agent_id:
                    from store_adapter import get_db
                    from libs.store import agent_inbox_push
                    db = get_db(STATE_DIR)
                    agent_inbox_push(db, bound_agent_id,
                        event_type='task_received',
                        payload={'instruction': message[:200], 'summary': full_text[:200]})
            except Exception:
                pass

        asyncio.run(_run())

    def _get_session_info(self) -> dict:
        """Build session info for the right-side pane.
        
        Three tabs:
        1. Session outcome ledger
        2. Estimated goal structure
        3. User model
        Plus token usage breakdown at the bottom.
        """
        info = {
            'tasks': [],
            'goals': [],
            'goal_summary': {
                'active_goal_id': '',
                'session_total': 0,
                'project_total': 0,
                'proposed': 0,
                'confirmed': 0,
                'executing': 0,
                'verifying': 0,
                'active': 0,
                'backlog': 0,
                'blocked': 0,
                'completed_recent': 0,
                'failed_recent': 0,
            },
            'user_model': '',
            'transfer': {},
            'binding': {
                'session_id': self._active_agent_id or '',
                'agent_id': getattr(self, '_bound_agent_id', None) or '',
                'mode': 'bound-agent' if getattr(self, '_bound_agent_id', None) else 'fresh-session',
            },
            'tokens': {
                'chat_in': 0,
                'chat_out': 0,
                'summary_tokens': 0,
                'goal_inference_tokens': int(getattr(self, '_goal_inference_token_estimate', 0) or 0),
                'consolidation_tokens': 0,
                'max_context': 0,
            },
        }

        # Session-local outcome ledger
        if hasattr(self, '_session_tasks'):
            info['tasks'] = self._session_tasks[-50:]
            info['tokens']['chat_in'] = sum(t.get('tokens_in', 0) for t in self._session_tasks)
            info['tokens']['chat_out'] = sum(t.get('tokens_out', 0) for t in self._session_tasks)

        try:
            if self.engine and getattr(self.engine, 'model', None):
                info['tokens']['max_context'] = int(getattr(self.engine.model, 'context_window', 0) or 0)
        except Exception:
            pass

        # Goals: session-level + project-level
        try:
            from goal_runtime import list_goals, _safe_id, _read_json, _session_path, _default_session_doc
            onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
            project = str(onboarding.get('project') or str(ROOT)).strip()
            import time as _time
            cutoff = _time.time() - 86400

            # Session goals (current session)
            if self._active_agent_id:
                session_id = _safe_id(self._active_agent_id, 'session')
                ses_doc = _read_json(_session_path(STATE_DIR, session_id), {})
                ses_goals = [g for g in (ses_doc.get('goals') or []) if isinstance(g, dict)]
                info['goal_summary']['active_goal_id'] = str(ses_doc.get('active_goal_id') or '')
                info['goal_summary']['session_total'] = len(ses_goals)
                for g in ses_goals[-10:]:
                    info['goals'].append({
                        'id': g.get('goal_id', ''),
                        'title': g.get('title', '')[:80],
                        'status': g.get('status', ''),
                        'intent_type': g.get('intent_type', ''),
                        'criteria': g.get('acceptance_criteria', []),
                        'scope': 'session',
                    })

            # Project goals (active/recent only, skip duplicates from session)
            session_ids = {g['id'] for g in info['goals']}
            all_goals = list_goals(STATE_DIR, project=project)
            info['goal_summary']['project_total'] = len(all_goals)
            for g in all_goals:
                status = str(g.get('status') or '')
                if status == 'proposed':
                    info['goal_summary']['proposed'] += 1
                elif status == 'confirmed':
                    info['goal_summary']['confirmed'] += 1
                elif status == 'executing':
                    info['goal_summary']['executing'] += 1
                elif status == 'verifying':
                    info['goal_summary']['verifying'] += 1
                elif status == 'active':
                    info['goal_summary']['active'] += 1
                elif status == 'backlog':
                    info['goal_summary']['backlog'] += 1
                elif status == 'blocked':
                    info['goal_summary']['blocked'] += 1
                elif status == 'completed':
                    info['goal_summary']['completed_recent'] += 1
                elif status == 'failed':
                    info['goal_summary']['failed_recent'] += 1
            stale_cutoff = _time.time() - 7 * 86400  # 7 days
            stale_iso = __import__('datetime').datetime.fromtimestamp(stale_cutoff).isoformat()
            all_goals = [g for g in all_goals if 
                g.get('goal_id', '') not in session_ids and (
                    (g.get('status') in ('active', 'backlog', 'proposed', 'confirmed') 
                     and g.get('created_at', '') > stale_iso) or
                    (g.get('status') == 'completed' and g.get('completed_at', '') > 
                        __import__('datetime').datetime.fromtimestamp(cutoff).isoformat())
                )]
            for g in all_goals[-10:]:
                info['goals'].append({
                    'id': g.get('goal_id', ''),
                    'title': g.get('title', '')[:80],
                    'status': g.get('status', ''),
                    'intent_type': g.get('intent_type', ''),
                    'criteria': g.get('acceptance_criteria', []),
                    'scope': 'project',
                })
        except Exception:
            pass

        # User model (rendered)
        try:
            from user_model_structured import load_structured, render_for_prompt
            model = load_structured(STATE_DIR)
            info['user_model'] = render_for_prompt(model)
        except Exception:
            pass

        # Active transfer metadata, if current engine was resumed via transfer
        try:
            if self.engine and getattr(self.engine, 'transfer_bundle', None):
                bundle = self.engine.transfer_bundle
                compiled = getattr(self.engine, 'transfer_compiled', {}) or {}
                info['transfer'] = {
                    'id': bundle.get('id', ''),
                    'source_provider': bundle.get('source', {}).get('provider', ''),
                    'target_provider': bundle.get('target', {}).get('provider', ''),
                    'full_message_count': bundle.get('history', {}).get('full_message_count', 0),
                    'full_transcript_path': bundle.get('history', {}).get('full_transcript_path', ''),
                    'profile_name': compiled.get('profile_name', ''),
                    'tier': compiled.get('tier', ''),
                    'budget_tokens': compiled.get('budget_tokens', 0),
                    'applied_tokens_estimate': compiled.get('applied_tokens_estimate', 0),
                    'replayed_messages': compiled.get('replayed_messages', 0),
                    'tool_history_mode': compiled.get('strategy', {}).get('tool_history_mode', ''),
                    'message_mode': compiled.get('strategy', {}).get('message_mode', ''),
                    'omitted': compiled.get('omitted', {}),
                }
        except Exception:
            pass

        # Token usage from consolidation traces
        try:
            from consolidation import list_traces
            traces = list_traces(STATE_DIR, limit=10)
            # Rough estimate: each consolidation uses ~1K tokens
            info['tokens']['consolidation_tokens'] = len(traces) * 1000
        except Exception:
            pass

        return info

    def _get_batch_progress(self) -> str:
        """Short progress string for active batches."""
        try:
            from batch_orchestrator import list_batches
            running = [b for b in list_batches(STATE_DIR) if b.get('status') == 'running']
            if not running:
                return ''
            total_done = sum(b.get('completed_count', 0) for b in running)
            total_all = sum(b.get('total', 0) for b in running)
            total_failed = sum(b.get('failed_count', 0) for b in running)
            parts = [f'({total_done}/{total_all})']
            if total_failed:
                parts.append(f'{total_failed}✗')
            if len(running) > 1:
                parts.append(f'{len(running)} batches')
            return ' '.join(parts)
        except Exception:
            return ''

    def _chat_worker(self, message: str, request_id: str | None):
        """Run handle_chat on a worker thread."""
        try:
            self.handle_chat(message, request_id)
        finally:
            self._chat_busy = False

    def _start_background_worker(self):
        """Start a daemon thread that runs periodic background tasks.

        Runs consolidation, goal inference, and emits heartbeat events
        even while the chat engine is busy processing a message.
        """
        def _bg_loop():
            import time as _time
            cycle = 0
            consolidation_interval = 50   # ~100 seconds
            goal_inference_interval = 30  # ~60 seconds
            last_consolidation = 0
            last_goal_inference = 0

            while True:
                _time.sleep(2)
                cycle += 1

                # Heartbeat event for the run log (so dashboard activity picks it up)
                if cycle % 30 == 0:
                    try:
                        from store_adapter import get_db
                        from libs.store import run_log_append
                        db = get_db(STATE_DIR)
                        run_log_append(db, 'heartbeat', cycle=cycle,
                                       uptime_seconds=cycle * 2)
                    except Exception:
                        pass

                # Consolidation check
                if cycle - last_consolidation >= consolidation_interval:
                    last_consolidation = cycle
                    try:
                        from consolidation import load_config, should_run, run_consolidation
                        config = load_config(STATE_DIR)
                        if config.get('enabled', True) and should_run(STATE_DIR, config):
                            result = run_consolidation(STATE_DIR, config)
                            changes = result.get('changes', [])
                            if changes:
                                emit({
                                    'type': 'status',
                                    'message': f'🧠 User model updated: {len(changes)} change(s)',
                                })
                    except Exception:
                        pass

                # Goal inference — always runs when there are enough messages
                # (independent of autonomous mode, which controls self-assignment)
                if cycle - last_goal_inference >= goal_inference_interval:
                    last_goal_inference = cycle
                    try:
                        from autonomous import (
                            load_autonomous_config, infer_goals_from_conversation,
                            propose_goal, get_proposed_goals,
                        )
                        auto_config = load_autonomous_config(STATE_DIR)
                        if (self.engine and
                                len(self.engine.messages) >= 4 and
                                not self._chat_busy):
                            # Only infer if there are enough messages and we're not mid-chat
                            import asyncio as _aio
                            onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                            project = str(onboarding.get('project') or str(ROOT)).strip()

                            # Check if we already have proposed goals waiting
                            existing_proposed = get_proposed_goals(STATE_DIR, project=project)
                            if len(existing_proposed) < 3:  # don't spam proposals
                                from provider_bridge import create_provider_and_model
                                provider, model, ready = create_provider_and_model(STATE_DIR)
                                if ready:
                                    self._goal_inference_token_estimate += 1000
                                    goals = _aio.run(infer_goals_from_conversation(
                                        STATE_DIR,
                                        agent_id=self._active_agent_id or '',
                                        messages=self.engine.messages,
                                        provider=provider,
                                        model=model,
                                    ))
                                    for g in goals[:2]:  # max 2 proposals per cycle
                                        proposed = propose_goal(
                                            STATE_DIR,
                                            agent_id=self._active_agent_id or '',
                                            project=project,
                                            title=g.get('title', ''),
                                            acceptance_criteria=g.get('acceptance_criteria', []),
                                            plan=g.get('plan', []),
                                        )
                                        emit({
                                            'type': 'status',
                                            'message': (
                                                f'💡 Goal proposed: {proposed["title"][:80]}\n'
                                                f'   /confirm to approve, /reject to defer'
                                            ),
                                        })
                                        emit({'type': 'refresh', 'payload': {'session_info': self._get_session_info()}})
                    except Exception:
                        pass

                # Process queued shade phase + cron tasks (when not chatting)
                if cycle % 3 == 0 and not self._chat_busy:
                    try:
                        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                        import uuid as _uuid
                        from conversation_runtime import load_queue, save_queue
                        queue = load_queue(STATE_DIR)
                        now_iso = _dt.now(_tz.utc).isoformat()

                        def _is_due(t: dict) -> bool:
                            nb = t.get('not_before')
                            return (not nb) or (str(nb) <= now_iso)

                        def _is_cron_task(t: dict) -> bool:
                            return str(t.get('correlation_id') or '').startswith('cron:')

                        pending = [
                            t for t in queue
                            if t.get('status') == 'pending'
                            and _is_due(t)
                            and (t.get('shade_phase') or _is_cron_task(t))
                        ]

                        if pending:
                            task = pending[0]
                            agent_id = task.get('owner_agent_id') or task.get('actor_agent_id', '')
                            if agent_id:
                                from agent_lifecycle import list_agents
                                agent = None
                                for a in list_agents():
                                    if a.get('id') == agent_id:
                                        agent = a
                                        break
                                if agent:
                                    task['status'] = 'in_progress'
                                    task['started_at'] = now_iso
                                    save_queue(STATE_DIR, queue)

                                    from agent_runtime import run_task_tick
                                    ok, result = run_task_tick(STATE_DIR, task, agent=agent)

                                    task['status'] = 'completed' if ok else 'failed'
                                    if ok:
                                        task['result_summary'] = (result or {}).get('summary', '')
                                    else:
                                        task['last_error'] = result
                                    task['completed_at'] = _dt.now(_tz.utc).isoformat()
                                    task['updated_at'] = task['completed_at']
                                    save_queue(STATE_DIR, queue)

                                    # TUI-side recurrence for cron tasks, mirroring charon_loop behavior.
                                    if ok and _is_cron_task(task):
                                        interval = task.get('interval_minutes')
                                        if interval and isinstance(interval, (int, float)) and interval > 0:
                                            next_run = _dt.now(_tz.utc) + _td(minutes=float(interval))
                                            recurring_copy = {
                                                'id': f"{str(task.get('id') or 'task').split('-')[0]}-{_uuid.uuid4().hex[:8]}",
                                                'title': task.get('title', ''),
                                                'instruction': task.get('instruction', ''),
                                                'status': 'pending',
                                                'task_type': task.get('task_type', 'agent_task'),
                                                'owner_agent_id': task.get('owner_agent_id'),
                                                'actor_agent_id': task.get('actor_agent_id'),
                                                'project': task.get('project'),
                                                'priority': task.get('priority', 'normal'),
                                                'created_at': _dt.now(_tz.utc).isoformat(),
                                                'updated_at': _dt.now(_tz.utc).isoformat(),
                                                'attempt_count': 0,
                                                'max_attempts': int(task.get('max_attempts') or 3),
                                                'interval_minutes': interval,
                                                'not_before': next_run.isoformat(),
                                                'correlation_id': task.get('correlation_id'),
                                                'constraints': task.get('constraints') or [],
                                                'expected_outputs': task.get('expected_outputs') or [],
                                            }
                                            queue.append(recurring_copy)
                                            save_queue(STATE_DIR, queue)
                    except Exception:
                        pass

                # Monitor batches and report completion
                if cycle % 5 == 0:  # check every 10 seconds
                    try:
                        from batch_orchestrator import list_batches, summarize_batch, get_batch
                        # _notified_batches is pre-populated at startup

                        all_batches = list_batches(STATE_DIR)
                        has_running = False
                        for b in all_batches:
                            bid = b.get('id', '')
                            status = b.get('status', '')

                            if status == 'running':
                                has_running = True

                            # Notify on completion (only once per batch)
                            if status in ('completed', 'partial') and bid not in self._notified_batches:
                                self._notified_batches.add(bid)
                                done = b.get('completed_count', 0)
                                failed = b.get('failed_count', 0)
                                total = b.get('total', 0)

                                # Build per-task results
                                lines = [f'⚡ Batch complete: {summarize_batch(b)}']
                                for t in b.get('tasks', []):
                                    icon = '✓' if t.get('status') == 'completed' else '✗'
                                    summary = (t.get('result_summary') or t.get('error') or '')[:60]
                                    lines.append(f'  {icon} {t.get("title", "")}: {summary}')

                                emit({'type': 'status', 'message': '\n'.join(lines)})

                        if not has_running and self.agent_mode == 'delegating':
                            self.agent_mode = 'interactive'
                    except Exception:
                        pass

                    # Update agent mode based on state
                    try:
                        from batch_orchestrator import list_batches
                        from autonomous import load_autonomous_config
                        running_batches = list_batches(STATE_DIR, status='running')
                        auto_cfg = load_autonomous_config(STATE_DIR)

                        if self._chat_busy:
                            # User is actively chatting — always interactive
                            # (batch progress still shows separately in status bar)
                            self.agent_mode = 'interactive'
                        elif running_batches:
                            self.agent_mode = 'delegating'
                        elif auto_cfg.get('enabled'):
                            self.agent_mode = 'autonomous'
                        else:
                            self.agent_mode = 'idle' if not self.engine else 'interactive'
                    except Exception:
                        pass

        t = threading.Thread(target=_bg_loop, daemon=True)
        t.start()

    def _save_conversation_now(self):
        """Save current conversation state immediately (called on exit)."""
        if self._active_agent_id and self.engine and self.engine.messages:
            try:
                from conversation_store import save_conversation, message_to_dict
                # Use full history from lossless store when available
                msgs_to_save = None
                if self.engine.has_lossless_store:
                    msgs_to_save = _full_messages_from_store(self._active_agent_id)
                if msgs_to_save is None:
                    msgs_to_save = list(self.engine.messages)
                save_conversation(STATE_DIR, self._active_agent_id,
                    [message_to_dict(m) for m in msgs_to_save])
            except Exception:
                pass
        # Unregister live session
        if self._active_agent_id:
            try:
                from session_registry import unregister_session
                unregister_session(STATE_DIR, self._active_agent_id)
            except Exception:
                pass

    def handle_abort(self, request_id: str | None):
        stopped_tool = False
        try:
            from tools import abort_running_bash
            stopped_tool = abort_running_bash()
        except Exception:
            stopped_tool = False
        if self.engine:
            self.engine.abort()
            msg = 'Aborted current run.'
            if stopped_tool:
                msg += ' Active bash command killed.'
            emit({'type': 'status', 'message': msg, 'request_id': request_id})
        elif stopped_tool:
            emit({'type': 'status', 'message': 'Killed active bash command.', 'request_id': request_id})

    def handle_steer(self, message: str, request_id: str | None):
        """Interrupt the agent mid-execution with a new instruction."""
        if self.engine:
            self.engine.steer(message)
            emit({'type': 'steer_queued', 'message': message,
                  'pending': self.engine.pending_messages,
                  'request_id': request_id})
        else:
            emit({'type': 'error', 'error': 'No active engine to steer.',
                  'request_id': request_id})

    def handle_follow_up(self, message: str, request_id: str | None):
        """Queue a message for after the agent finishes."""
        if self.engine:
            self.engine.follow_up(message)
            emit({'type': 'follow_up_queued', 'message': message,
                  'pending': self.engine.pending_messages,
                  'request_id': request_id})
        else:
            emit({'type': 'error', 'error': 'No active engine for follow-up.',
                  'request_id': request_id})

    def run(self):
        # Check if already set up — auto-initialize if so
        onboarding = self._repair_incomplete_onboarding_startup()
        requested_provider = os.environ.get('CHARON_PROVIDER', '').strip()
        requested_resume = os.environ.get('CHARON_RESUME', '').strip()
        requested_agent = os.environ.get('CHARON_AGENT', '').strip()

        # Resume a specific agent's conversation
        if requested_resume:
            try:
                from conversation_store import load_conversation, list_conversations
                if requested_resume == 'latest':
                    convos = list_conversations(STATE_DIR)
                    if convos:
                        convos.sort(key=lambda c: c.get('last_timestamp', 0), reverse=True)
                        requested_resume = convos[0]['agent_id']
                if requested_resume and requested_resume != 'latest':
                    self._active_agent_id = requested_resume
                    self._load_tasks_from_ledger(requested_resume)
                    emit({'type': 'status', 'message': f'Resuming conversation with {requested_resume}...'})
            except Exception:
                pass

        if onboarding.get('complete') and not requested_provider:
            # Already configured, no specific provider requested — ensure engine is ready
            try:
                self._ensure_engine()
                if requested_agent:
                    emit({'type': 'status', 'message': f'Started fresh session bound to agent {requested_agent}.'})
            except Exception:
                pass
            # Silently ensure an agent exists
            try:
                from agent_lifecycle import list_agents, create_agent
                existing = list_agents()
                has_charon = any(
                    a.get('role') == 'charon' and a.get('status') != 'stopped'
                    for a in existing
                )
                if not has_charon:
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    create_agent(
                        name='', mode='persistent',
                        goal=f'Primary agent for {project.split("/")[-1] or "project"}',
                        project=project, role='charon', visibility='user',
                        require_tmux=False,
                    )
            except Exception:
                pass
        elif requested_provider:
            # Specific provider requested (e.g. charon claude-code)
            # Auto-start onboarding for that provider
            emit({'type': 'status', 'message': f'Starting with provider: {requested_provider}'})
            self.handle_command(f'/setup provider {requested_provider}', None)
        self.handle_refresh(None)

        # Pre-populate notified batches so old completions don't spam on startup
        try:
            from batch_orchestrator import list_batches
            for b in list_batches(STATE_DIR):
                if b.get('status') in ('completed', 'partial'):
                    self._notified_batches.add(b.get('id', ''))
        except Exception:
            pass

        # Save conversation and cleanup owned sessions on exit
        import atexit
        atexit.register(self._cleanup_owned_sessions)
        atexit.register(self._save_conversation_now)

        def _shutdown_handler(signum, frame):
            self._cleanup_owned_sessions()
            self._save_conversation_now()
            raise SystemExit(0)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _shutdown_handler)
            except Exception:
                pass

        # Start background worker for consolidation, goal inference, etc.
        self._chat_busy = False
        self._start_background_worker()

        while True:
            try:
                line = sys.stdin.buffer.readline()
            except (EOFError, KeyboardInterrupt):
                self._cleanup_owned_sessions()
                self._save_conversation_now()
                break
            if not line:
                self._cleanup_owned_sessions()
                self._save_conversation_now()
                break

            try:
                msg = json.loads(line.decode('utf-8'))
            except Exception:
                continue

            req_type = msg.get('type', '')
            request_id = msg.get('request_id')

            if req_type == 'chat':
                # Run chat on a worker thread so main loop stays responsive
                self._chat_busy = True
                t = threading.Thread(target=self._chat_worker, args=(msg.get('message', ''), request_id), daemon=True)
                t.start()
            elif req_type == 'command':
                self.handle_command(msg.get('command', ''), request_id)
            elif req_type == 'refresh':
                self.handle_refresh(request_id)
            elif req_type == 'abort':
                self.handle_abort(request_id)
            elif req_type == 'task_detail':
                task_id = msg.get('task_id', '')
                for t in getattr(self, '_session_tasks', []):
                    if t.get('task_id') == task_id:
                        emit({
                            'type': 'task_detail',
                            'task_id': task_id,
                            'detail': t.get('detail', t.get('summary', '')),
                            'request_id': request_id,
                        })
                        break
            elif req_type == 'approval_response':
                try:
                    from tools import respond_to_approval
                    respond_to_approval(msg.get('approved', False))
                except Exception:
                    pass
            elif req_type == 'steer':
                self.handle_steer(msg.get('message', ''), request_id)
            elif req_type == 'follow_up':
                self.handle_follow_up(msg.get('message', ''), request_id)
            elif req_type == 'send_steer':
                target = msg.get('target_session', '')
                steer_msg = msg.get('message', '')
                if target and steer_msg:
                    try:
                        from session_registry import send_steer
                        ok = send_steer(STATE_DIR, target, steer_msg)
                        emit({'type': 'status', 'message': f'📡 Sent to {target.split("-")[-1][:6]}: {steer_msg[:40]}', 'request_id': request_id})
                    except Exception as e:
                        emit({'type': 'error', 'error': f'Steer failed: {e}', 'request_id': request_id})
            elif req_type == 'live_conv':
                # Load conversation preview for a live session
                session_id = msg.get('session_id', '')
                if session_id:
                    try:
                        from conversation_store import load_conversation
                        msgs = load_conversation(STATE_DIR, session_id)
                        # Format conversation with tool calls, streaming feel
                        preview_lines = []
                        for m in msgs[-30:]:
                            role = m.get('role', '')
                            content = m.get('content', '')
                            tool_calls = m.get('tool_calls', [])
                            if role == 'user' and content:
                                preview_lines.append('')
                                for line in content.split('\n'):
                                    preview_lines.append(f'❯ {line}')
                            elif role == 'assistant':
                                if content:
                                    preview_lines.append('')
                                    for line in content.split('\n'):
                                        preview_lines.append(f'  {line}')
                                for tc in tool_calls:
                                    name = tc.get('name', '')
                                    args = tc.get('arguments', {})
                                    if name == 'Bash':
                                        preview_lines.append(f'  ⚡ {name}  {str(args.get("command",""))[:50]}')
                                    elif name == 'Read':
                                        preview_lines.append(f'  📄 {name}  {args.get("path","")}')
                                    elif name == 'Write':
                                        preview_lines.append(f'  ✏️ {name}  {args.get("path","")}')
                                    elif name == 'Edit':
                                        preview_lines.append(f'  🔧 {name}  {args.get("path","")}')
                                    else:
                                        preview_lines.append(f'  ⚙ {name}')
                            elif role == 'tool_result':
                                tool = m.get('tool_name', '')
                                is_err = m.get('is_error', False)
                                first_line = (content or '').split('\n')[0][:50]
                                icon = '✗' if is_err else '✓'
                                preview_lines.append(f'    {icon} {first_line}')
                        emit({
                            'type': 'live_conv',
                            'session_id': session_id,
                            'preview': '\n'.join(preview_lines[-40:]),
                            'message_count': len(msgs),
                            'request_id': request_id,
                        })
                    except Exception:
                        pass
            elif req_type == 'tmux_capture':
                self.handle_tmux_capture(msg.get('session', ''), request_id)
            elif req_type == 'tmux_send':
                self.handle_tmux_send(msg.get('session', ''), msg.get('keys', ''), msg.get('literal', False), request_id)
            elif req_type == 'consolidation_traces':
                self.handle_consolidation_traces(request_id)
            elif req_type == 'consolidation_config':
                self.handle_consolidation_config(msg, request_id)
            elif req_type == 'consolidation_run':
                self.handle_consolidation_run(request_id)
            elif req_type == 'agent_ledger':
                self.handle_agent_ledger(msg.get('agent_id', ''), request_id)


    def handle_consolidation_traces(self, request_id: str | None):
        """Return recent consolidation scan traces for dashboard display."""
        try:
            from consolidation import list_traces
            traces = list_traces(STATE_DIR, limit=20)
            emit({
                'type': 'consolidation_traces',
                'traces': traces,
                'request_id': request_id,
            })
        except Exception as e:
            emit({'type': 'error', 'error': f'Failed to load traces: {e}', 'request_id': request_id})

    def handle_consolidation_config(self, msg: dict, request_id: str | None):
        """Get or update consolidation config."""
        from consolidation import load_config, save_config
        action = msg.get('action', 'get')
        if action == 'get':
            config = load_config(STATE_DIR)
            emit({
                'type': 'consolidation_config',
                'config': config,
                'request_id': request_id,
            })
        elif action == 'set':
            updates = msg.get('config', {})
            config = load_config(STATE_DIR)
            config.update(updates)
            save_config(STATE_DIR, config)
            emit({
                'type': 'consolidation_config',
                'config': config,
                'message': 'Config updated.',
                'request_id': request_id,
            })

    def handle_agent_ledger(self, agent_id: str, request_id: str | None):
        """Return task history for an agent."""
        if not agent_id:
            # Default to the primary charon agent
            try:
                from agent_lifecycle import list_agents
                for a in list_agents():
                    if a.get('role') == 'charon' and a.get('status') != 'stopped':
                        agent_id = a.get('id', '')
                        break
            except Exception:
                pass
        try:
            from task_ledger import get_agent_ledger_summary
            result = get_agent_ledger_summary(STATE_DIR, agent_id)
            emit({
                'type': 'agent_ledger',
                'agent_id': agent_id,
                'entries': result['entries'],
                'stats': result['stats'],
                'request_id': request_id,
            })
        except Exception as e:
            emit({'type': 'error', 'error': f'Ledger failed: {e}', 'request_id': request_id})

    def handle_consolidation_run(self, request_id: str | None):
        """Manually trigger a consolidation scan."""
        try:
            from consolidation import load_config, run_consolidation
            config = load_config(STATE_DIR)
            result = run_consolidation(STATE_DIR, config)
            changes = result.get('changes', [])
            emit({
                'type': 'consolidation_result',
                'trace': result,
                'message': f'Consolidation complete: {len(changes)} changes, {result.get("events_processed", 0)} events processed.',
                'request_id': request_id,
            })
        except Exception as e:
            emit({'type': 'error', 'error': f'Consolidation failed: {e}', 'request_id': request_id})

    def _detect_session_state(self, content: str) -> tuple[str, str]:
        """Heuristic: detect session state and generate summary from tmux content.
        Returns (state, summary).
        """
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        if not lines:
            return 'idle', 'empty session'

        last_lines = lines[-5:]
        last_text = ' '.join(last_lines).lower()

        # Waiting for input?
        if any(p in last_text for p in ['[y/n]', '(y/n)', 'confirm', 'approve', 'continue?', 'proceed?']):
            return 'waiting', 'waiting for confirmation'
        if last_text.rstrip().endswith('?'):
            return 'waiting', 'question pending'

        # Error?
        if any(p in last_text for p in ['error:', 'failed', 'traceback', 'exception', 'panic']):
            return 'running', 'error detected'

        # At a prompt? (idle)
        last_line = lines[-1].strip() if lines else ''
        # Strip ANSI for pattern matching
        import re
        clean_last = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', last_line)
        if clean_last.endswith('$') or clean_last.endswith('❯') or clean_last.endswith('>') or clean_last.endswith('#'):
            return 'idle', 'at prompt'

        # Agent working patterns
        if any(p in last_text for p in ['thinking', 'reading', 'writing', 'editing', 'running', 'searching']):
            return 'running', 'working...'
        if any(p in last_text for p in ['tool_call', 'bash', 'executing']):
            return 'running', 'executing tools'
        if any(p in last_text for p in ['streaming', 'generating', '...']):
            return 'running', 'generating response'

        return 'running', 'active'

    # Cache for session state detection
    _session_states: dict[str, tuple[str, str]] = {}
    _session_hashes: dict[str, str] = {}

    def handle_tmux_capture(self, session_name: str, request_id: str | None):
        """Capture tmux pane content for the session grid."""
        try:
            from tmux_capture import capture_pane
            content = capture_pane(session_name, width=120, height=40)

            # Detect state (only re-detect if content changed)
            import hashlib
            content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
            if self._session_hashes.get(session_name) != content_hash:
                self._session_hashes[session_name] = content_hash
                state, summary = self._detect_session_state(content)
                self._session_states[session_name] = (state, summary)

            state, summary = self._session_states.get(session_name, ('idle', ''))

            emit({
                'type': 'tmux_capture',
                'session': session_name,
                'content': content,
                'state': state,
                'summary': summary,
                'request_id': request_id,
            })
        except Exception as e:
            emit({'type': 'error', 'error': f'Capture failed: {e}', 'request_id': request_id})

    def handle_tmux_send(self, session_name: str, keys: str, literal: bool, request_id: str | None):
        """Send keys to a tmux session."""
        try:
            from tmux_capture import send_keys, send_key_literal
            if literal:
                ok = send_key_literal(session_name, keys)
            else:
                ok = send_keys(session_name, keys)
            emit({
                'type': 'tmux_send_result',
                'session': session_name,
                'ok': ok,
                'request_id': request_id,
            })
        except Exception as e:
            emit({'type': 'error', 'error': f'Send failed: {e}', 'request_id': request_id})


if __name__ == '__main__':
    backend = ChatBackend()
    backend.run()
