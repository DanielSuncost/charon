from __future__ import annotations

import base64
import json
import re
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from charon.conversation.conversation_participants import ConversationParticipantSpec, get_conversation_adapter

import uuid
from datetime import datetime, timezone
from charon.infra import config

# SQLite store adapter (optional)
try:
    from charon.infra.store_adapter import (
        get_db as _get_db,
        task_insert as _db_task_insert,
    )
    _HAS_STORE = True
except ImportError:
    _HAS_STORE = False


@dataclass
class ConversationTurnResult:
    ok: bool
    error: str = ''
    output: str = ''
    last_line: str = ''
    message_text: str = ''

    def as_dict(self) -> dict:
        return asdict(self)


def _use_store() -> bool:
    return _HAS_STORE and not config.no_sqlite()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _queue_path(state_dir: Path) -> Path:
    return Path(state_dir) / 'queue.json'


def _load_queue(state_dir: Path) -> list[dict]:
    path = _queue_path(state_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_queue(state_dir: Path, queue: list[dict]) -> None:
    path = _queue_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding='utf-8')


def load_queue(state_dir: Path) -> list[dict]:
    return _load_queue(state_dir)


def save_queue(state_dir: Path, queue: list[dict]) -> None:
    _save_queue(state_dir, queue)


def _enqueue_task(state_dir: Path, task: dict) -> dict:
    queue = _load_queue(state_dir)
    queue.append(task)
    _save_queue(state_dir, queue)
    if _use_store():
        try:
            _db_task_insert(_get_db(state_dir), dict(task))
        except Exception:
            pass
    return task


def enqueue_agent_task(
    state_dir: Path,
    *,
    owner_agent_id: str,
    instruction: str,
    title: str | None = None,
    project: str | None = None,
    priority: str = 'normal',
    scope: list[str] | None = None,
    deps: list[str] | None = None,
    correlation_id: str | None = None,
    interval_minutes: float | None = None,
    not_before: str | None = None,
    max_attempts: int = 3,
) -> dict:
    now = _utc_now_iso()
    instruction_text = str(instruction or '').strip()
    if title:
        title_text = str(title).strip()[:120]
    else:
        title_text = (instruction_text[:117] + '...') if len(instruction_text) > 120 else instruction_text
    owner = str(owner_agent_id or '').strip()
    task_id = f"task-{uuid.uuid4().hex[:10]}"
    task = {
        'id': task_id,
        'title': title_text,
        'instruction': instruction_text,
        'status': 'pending',
        'task_type': 'agent_task',
        'owner_agent_id': owner,
        'project': str(project or '').strip() or None,
        'priority': str(priority or 'normal').strip() or 'normal',
        'scope': list(scope or []),
        'deps': list(deps or []),
        'correlation_id': str(correlation_id) if correlation_id else task_id,
        'created_at': now,
        'updated_at': now,
        'attempt_count': 0,
        'max_attempts': int(max_attempts or 3),
        'boundary': {
            'status': 'unclaimed',
            'lease_owner': owner,
            'lease_expires_at': None,
            'overlap_with': [],
        },
    }
    if interval_minutes:
        task['interval_minutes'] = float(interval_minutes)
    if not_before:
        task['not_before'] = str(not_before)
    return _enqueue_task(state_dir, task)


def enqueue_agent_message_task(
    state_dir: Path,
    *,
    actor_agent_id: str,
    conversation_id: str,
    message: str,
    parent_message_id: str | None = None,
    branch_label: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    now = _utc_now_iso()
    task_id = f"task-{uuid.uuid4().hex[:10]}"
    task = {
        'id': task_id,
        'title': str(message or '').strip()[:120],
        'instruction': str(message or '').strip(),
        'message': str(message or '').strip(),
        'status': 'pending',
        'task_type': 'agent_message',
        'actor_agent_id': str(actor_agent_id or '').strip(),
        'conversation_id': str(conversation_id or '').strip(),
        'correlation_id': str(correlation_id) if correlation_id else task_id,
        'created_at': now,
        'updated_at': now,
        'attempt_count': 0,
        'max_attempts': 3,
    }
    if parent_message_id:
        task['parent_message_id'] = str(parent_message_id)
    if branch_label:
        task['branch_label'] = str(branch_label)
    return _enqueue_task(state_dir, task)


def enqueue_agent_intervention_task(
    state_dir: Path,
    *,
    actor_agent_id: str,
    conversation_id: str,
    intervention_of_message_id: str,
    message: str,
    parent_message_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    now = _utc_now_iso()
    task_id = f"task-{uuid.uuid4().hex[:10]}"
    task = {
        'id': task_id,
        'title': str(message or '').strip()[:120],
        'instruction': str(message or '').strip(),
        'message': str(message or '').strip(),
        'status': 'pending',
        'task_type': 'agent_intervention',
        'actor_agent_id': str(actor_agent_id or '').strip(),
        'conversation_id': str(conversation_id or '').strip(),
        'intervention_of_message_id': str(intervention_of_message_id or '').strip(),
        'correlation_id': str(correlation_id) if correlation_id else task_id,
        'created_at': now,
        'updated_at': now,
        'attempt_count': 0,
        'max_attempts': 3,
    }
    if parent_message_id:
        task['parent_message_id'] = str(parent_message_id)
    return _enqueue_task(state_dir, task)


def enqueue_boundary_proposal_task(
    state_dir: Path,
    *,
    proposer_agent_id: str,
    target_agent_id: str,
    project: str | None = None,
    scope: list[str] | None = None,
    reason: str = '',
    source_task_id: str | None = None,
    conversation_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    now = _utc_now_iso()
    task_id = f"task-{uuid.uuid4().hex[:10]}"
    task = {
        'id': task_id,
        'title': f"boundary proposal -> {str(target_agent_id or '').strip()}"[:120],
        'status': 'pending',
        'task_type': 'boundary_proposal',
        'actor_agent_id': str(proposer_agent_id or '').strip(),
        'target_agent_id': str(target_agent_id or '').strip(),
        'project': str(project or '').strip() or None,
        'scope': list(scope or []),
        'reason': str(reason or ''),
        'correlation_id': str(correlation_id) if correlation_id else task_id,
        'created_at': now,
        'updated_at': now,
        'attempt_count': 0,
        'max_attempts': 3,
    }
    if source_task_id:
        task['source_task_id'] = str(source_task_id)
    if conversation_id:
        task['conversation_id'] = str(conversation_id)
    return _enqueue_task(state_dir, task)


def enqueue_boundary_resolution_task(
    state_dir: Path,
    *,
    resolver_agent_id: str,
    proposal_id: str,
    decision: str,
    reason: str = '',
    conversation_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    now = _utc_now_iso()
    task_id = f"task-{uuid.uuid4().hex[:10]}"
    task = {
        'id': task_id,
        'title': f"boundary {str(decision or '').strip()}: {str(proposal_id or '').strip()}"[:120],
        'status': 'pending',
        'task_type': 'boundary_resolution',
        'actor_agent_id': str(resolver_agent_id or '').strip(),
        'proposal_id': str(proposal_id or '').strip(),
        'decision': str(decision or '').strip(),
        'reason': str(reason or ''),
        'correlation_id': str(correlation_id) if correlation_id else task_id,
        'created_at': now,
        'updated_at': now,
        'attempt_count': 0,
        'max_attempts': 3,
    }
    if conversation_id:
        task['conversation_id'] = str(conversation_id)
    return _enqueue_task(state_dir, task)


def list_conversations(state_dir: Path) -> list[dict]:
    """Read the conversation index file and return one row per conversation.

    Rows preserve the index's insertion order and carry the conversation_id
    alongside whatever metadata the index recorded (message_count, agents, ...).
    """
    index_file = Path(state_dir) / 'conversation_index.json'
    try:
        raw = json.loads(index_file.read_text(encoding='utf-8'))
    except Exception:
        return []
    conversations = raw.get('conversations') if isinstance(raw, dict) else None
    if not isinstance(conversations, dict):
        return []
    rows = []
    for conv_id, meta in conversations.items():
        row = {'conversation_id': conv_id}
        if isinstance(meta, dict):
            row.update(meta)
        rows.append(row)
    return rows


def enqueue_user_intent_task(
    state_dir: Path,
    *,
    actor_agent_id: str,
    message: str,
    project: str | None = None,
    priority: str = 'normal',
    conversation_id: str | None = None,
    session_id: str | None = None,
    max_attempts: int = 3,
) -> dict:
    now = _utc_now_iso()
    text = str(message or '').strip()
    # Stored under 'owner_agent_id' to match how charon_loop consumes
    # user_intent tasks (it reads owner_agent_id first, then actor_agent_id).
    owner = str(actor_agent_id or '').strip()
    task = {
        'id': f"task-{uuid.uuid4().hex[:10]}",
        'title': (text[:117] + '...') if len(text) > 120 else text,
        'instruction': text,
        'message': text,
        'status': 'pending',
        'task_type': 'user_intent',
        'owner_agent_id': owner,
        'actor_agent_id': owner,
        'project': str(project or '').strip() or None,
        'priority': str(priority or 'normal').strip() or 'normal',
        'created_at': now,
        'updated_at': now,
        'attempt_count': 0,
        'max_attempts': int(max_attempts or 3),
    }
    if conversation_id:
        task['conversation_id'] = str(conversation_id)
    if session_id:
        task['session_id'] = str(session_id)
    return _enqueue_task(state_dir, task)


class ConversationSessionRuntime:
    def wait_until_ready(self, timeout: float = 15.0) -> bool:
        raise NotImplementedError

    def send_input(self, text: str) -> bool:
        raise NotImplementedError

    def capture_output(
        self,
        *,
        timeout: float = 20.0,
        prompt_hint: str = '',
        quiet_period: float = 0.9,
        completion_timeout: float = 10.0,
        on_event: Callable[[dict], None] | None = None,
    ) -> ConversationTurnResult:
        raise NotImplementedError

    def prompt_and_capture(
        self,
        text: str,
        *,
        timeout: float = 20.0,
        quiet_period: float = 0.9,
        completion_timeout: float = 10.0,
        on_event: Callable[[dict], None] | None = None,
    ) -> ConversationTurnResult:
        if not self.send_input(text):
            return ConversationTurnResult(ok=False, error='failed to send input')
        return self.capture_output(
            timeout=timeout,
            prompt_hint=text,
            quiet_period=quiet_period,
            completion_timeout=completion_timeout,
            on_event=on_event,
        )


class BoatConversationRuntime(ConversationSessionRuntime):
    def __init__(self, session_name: str):
        self.session_name = str(session_name or '').strip()

    def _socket_path(self) -> str | None:
        if not self.session_name:
            return None
        reg = Path.home() / '.charon' / 'boats' / f'{self.session_name}.json'
        if not reg.exists():
            return None
        try:
            data = json.loads(reg.read_text())
        except Exception:
            return None
        sock = str(data.get('socket') or '').strip()
        return sock or None

    def wait_until_ready(self, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            sock = self._socket_path()
            if sock and Path(sock).exists():
                return True
            time.sleep(0.2)
        return False

    def send_input(self, text: str) -> bool:
        sock_path = self._socket_path()
        if not sock_path:
            return False
        try:
            payload = base64.b64encode((text or '').encode('utf-8')).decode('ascii')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(2.0)
                sock.connect(sock_path)
                sock.sendall((json.dumps({'type': 'input', 'data': payload}) + '\n').encode('utf-8'))
            return True
        except Exception:
            return False

    def capture_output(
        self,
        *,
        timeout: float = 20.0,
        prompt_hint: str = '',
        quiet_period: float = 0.9,
        completion_timeout: float = 10.0,
        on_event: Callable[[dict], None] | None = None,
    ) -> ConversationTurnResult:
        sock_path = self._socket_path()
        if not sock_path:
            return ConversationTurnResult(ok=False, error='missing socket')
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
                last_progress_emit = 0.0
                reply_seen_at: float | None = None
                last_reply_change_at: float | None = None
                first_reply_text = ''
                while True:
                    now = time.time()
                    if meaningful_since is None and now >= start_deadline:
                        break
                    if meaningful_since is not None and completion_deadline is not None and now >= completion_deadline:
                        aggregate = ''.join(chunks)
                        meaningful = _extract_meaningful_text(aggregate, prompt_hint)
                        return ConversationTurnResult(
                            ok=True,
                            output=aggregate[-4000:],
                            last_line=_last_visible_line(meaningful),
                            message_text=meaningful,
                        )
                    try:
                        line = reader.readline()
                    except socket.timeout:
                        now = time.time()
                        if last_reply_change_at is not None and (now - last_reply_change_at) >= quiet_period:
                            aggregate = ''.join(chunks)
                            meaningful = _extract_meaningful_text(aggregate, prompt_hint)
                            return ConversationTurnResult(
                                ok=True,
                                output=aggregate[-4000:],
                                last_line=_last_visible_line(meaningful),
                                message_text=meaningful,
                            )
                        if meaningful_since is not None and reply_seen_at is None and (now - last_data_at) >= quiet_period:
                            aggregate = ''.join(chunks)
                            meaningful = _extract_meaningful_text(aggregate, prompt_hint)
                            return ConversationTurnResult(
                                ok=True,
                                output=aggregate[-4000:],
                                last_line=_last_visible_line(meaningful),
                                message_text=meaningful,
                            )
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
                    tool_info = _extract_tool_activity_info(text_chunk)
                    if on_event and tool_info is not None:
                        now = time.time()
                        if now - last_progress_emit >= 0.35:
                            on_event({
                                'type': 'tool_progress',
                                'session': self.session_name,
                                'summary': tool_info.get('summary') or _summarize_tool_activity(text_chunk),
                                'tool_name': tool_info.get('tool_name') or '',
                                'tool_phase': tool_info.get('tool_phase') or '',
                            })
                            last_progress_emit = now
                    if has_real_response:
                        now = time.time()
                        if meaningful_since is None:
                            meaningful_since = now
                            if on_event:
                                on_event({'type': 'reply_started', 'session': self.session_name, 'text': meaningful[:400]})
                        if meaningful != first_reply_text:
                            first_reply_text = meaningful
                            reply_seen_at = now if reply_seen_at is None else reply_seen_at
                            last_reply_change_at = now
                            if on_event and now - last_progress_emit >= 0.35:
                                on_event({'type': 'reply_progress', 'session': self.session_name, 'text': meaningful[:800]})
                                last_progress_emit = now
                        completion_deadline = now + min(completion_timeout, 4.0)
                meaningful = _extract_meaningful_text(''.join(chunks), prompt_hint)
                return ConversationTurnResult(
                    ok=False,
                    error='timeout waiting for output',
                    output=''.join(chunks)[-4000:],
                    last_line=_last_visible_line(meaningful),
                    message_text=meaningful,
                )
        except Exception as exc:
            return ConversationTurnResult(ok=False, error=str(exc))


class CharonNativeConversationRuntime(ConversationSessionRuntime):
    def __init__(self, participant: ConversationParticipantSpec):
        self.participant = participant

    def wait_until_ready(self, timeout: float = 15.0) -> bool:
        return False

    def send_input(self, text: str) -> bool:
        return False

    def capture_output(self, **kwargs) -> ConversationTurnResult:
        return ConversationTurnResult(ok=False, error='charon native conversation runtime not wired yet')


def runtime_for_participant(participant: dict | ConversationParticipantSpec) -> ConversationSessionRuntime:
    spec = participant if isinstance(participant, ConversationParticipantSpec) else ConversationParticipantSpec(
        id=str((participant or {}).get('id') or ''),
        name=str((participant or {}).get('name') or ''),
        role=str((participant or {}).get('role') or 'participant'),
        agent_type=str((participant or {}).get('agent_type') or (participant or {}).get('provider') or ''),
        provider=str((participant or {}).get('provider') or (participant or {}).get('agent_type') or ''),
        model=str((participant or {}).get('model') or ''),
        transport=str((participant or {}).get('transport') or ''),
        session=str((participant or {}).get('session') or ''),
        socket=str((participant or {}).get('socket') or ''),
        meta=dict((participant or {}).get('meta') or {}),
    )
    adapter = get_conversation_adapter(spec.agent_type or spec.provider or '')
    if adapter and getattr(adapter.capabilities, 'native_boat', False):
        return CharonNativeConversationRuntime(spec)
    return BoatConversationRuntime(spec.session)


def _strip_ansi(text: str) -> str:
    return re.sub(r'\x1b\[[0-9;?]*[ -/]*[@-~]', '', str(text or ''))


def _normalize_visible_text(text: str) -> str:
    cleaned = _strip_ansi(text).replace('\r', '\n')
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip().lower()


def _last_visible_line(text: str) -> str:
    cleaned = _strip_ansi(text).replace('\r', '\n')
    lines = [ln.strip() for ln in cleaned.split('\n') if ln.strip()]
    return lines[-1] if lines else ''


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


def _extract_tool_activity_info(text: str) -> dict | None:
    cleaned = _strip_ansi(text).replace('\r', '\n')
    norm = _normalize_visible_text(cleaned)
    tool_name = ''
    tool_phase = ''

    name_patterns = [
        r'\b(browser_[a-z_]+)\b',
        r'\b(read|edit|write|grep|sed|bash|python)\b',
    ]
    for pat in name_patterns:
        m = re.search(pat, norm)
        if m:
            tool_name = m.group(1)
            break

    phase_patterns = [
        ('navigating', r'\bnavigat(?:e|ing)\b'),
        ('snapshot', r'\bsnapshot\b'),
        ('vision', r'\bvision\b'),
        ('searching', r'\bsearch(?:ing)?\b'),
        ('fetching', r'\bfetch(?:ing)?\b'),
        ('scrolling', r'\bscroll(?:ing)?\b'),
        ('typing', r'\btyped?\b'),
        ('clicking', r'\bclicked?\b'),
        ('executing', r'\btool[_ ]call\b|\bbash\b|\bpython\b|\bread\b|\bedit\b|\bwrite\b'),
    ]
    for label, pat in phase_patterns:
        if re.search(pat, norm):
            tool_phase = label
            break

    if not tool_name and not tool_phase:
        return None

    summary = _summarize_tool_activity(cleaned)
    if tool_name and tool_phase:
        summary = f'{tool_name} ({tool_phase})'
    elif tool_name:
        summary = tool_name
    elif tool_phase:
        summary = tool_phase
    return {
        'tool_name': tool_name,
        'tool_phase': tool_phase,
        'summary': summary,
    }


def _summarize_tool_activity(text: str) -> str:
    cleaned = _strip_ansi(text).replace('\r', '\n')
    for raw in reversed(cleaned.split('\n')):
        line = raw.strip()
        if not line:
            continue
        if len(line) > 240:
            return line[:237] + '...'
        return line
    return 'tool activity'


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
