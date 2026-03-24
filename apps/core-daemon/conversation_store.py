"""Conversation persistence — save and load chat history per agent.

Each agent's conversation is stored as a JSONL file in
.charon_state/conversations/<agent-id>.jsonl

Each line is a message: {role, content, tool_calls, thinking, timestamp}
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _conv_dir(state_dir: Path) -> Path:
    d = state_dir / 'conversations'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _conv_path(state_dir: Path, agent_id: str) -> Path:
    return _conv_dir(state_dir) / f'{agent_id}.jsonl'


def save_message(state_dir: Path, agent_id: str, message: dict) -> None:
    """Append a single message to the conversation log."""
    path = _conv_path(state_dir, agent_id)
    with path.open('a') as f:
        f.write(json.dumps(message, ensure_ascii=False) + '\n')


def save_conversation(state_dir: Path, agent_id: str, messages: list[dict]) -> None:
    """Save the entire conversation (overwrite)."""
    path = _conv_path(state_dir, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w') as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + '\n')


def load_conversation(state_dir: Path, agent_id: str) -> list[dict]:
    """Load conversation history for an agent."""
    path = _conv_path(state_dir, agent_id)
    if not path.exists():
        return []
    messages = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(json.loads(line))
        except Exception:
            continue
    return messages


def list_conversations(state_dir: Path) -> list[dict]:
    """List all saved conversations with metadata."""
    conv_dir = _conv_dir(state_dir)
    result = []
    for f in sorted(conv_dir.glob('*.jsonl')):
        agent_id = f.stem
        try:
            lines = f.read_text().splitlines()
            msg_count = len([l for l in lines if l.strip()])
            # Get last message timestamp
            last_ts = 0
            for line in reversed(lines):
                if line.strip():
                    try:
                        msg = json.loads(line)
                        last_ts = msg.get('timestamp', 0)
                        break
                    except Exception:
                        pass
            result.append({
                'agent_id': agent_id,
                'message_count': msg_count,
                'last_timestamp': last_ts,
                'path': str(f),
            })
        except Exception:
            continue
    return result


def message_to_dict(msg: Any) -> dict:
    """Convert a providers.Message to a serializable dict."""
    d = {
        'role': getattr(msg, 'role', ''),
        'content': getattr(msg, 'content', ''),
        'timestamp': getattr(msg, 'timestamp', time.time()),
    }
    thinking = getattr(msg, 'thinking', '')
    if thinking:
        d['thinking'] = thinking
    tool_calls = getattr(msg, 'tool_calls', [])
    if tool_calls:
        d['tool_calls'] = [
            {'id': tc.id, 'name': tc.name, 'arguments': tc.arguments}
            for tc in tool_calls
        ]
    tool_call_id = getattr(msg, 'tool_call_id', None)
    if tool_call_id:
        d['tool_call_id'] = tool_call_id
        d['tool_name'] = getattr(msg, 'tool_name', '')
        d['is_error'] = getattr(msg, 'is_error', False)
    return d


def dict_to_message(d: dict) -> Any:
    """Convert a dict back to a providers.Message."""
    from providers import Message, ToolCall
    tool_calls = []
    for tc in d.get('tool_calls', []):
        tool_calls.append(ToolCall(
            id=tc.get('id', ''),
            name=tc.get('name', ''),
            arguments=tc.get('arguments', {}),
        ))
    return Message(
        role=d.get('role', ''),
        content=d.get('content', ''),
        tool_calls=tool_calls,
        tool_call_id=d.get('tool_call_id'),
        tool_name=d.get('tool_name'),
        is_error=d.get('is_error', False),
        thinking=d.get('thinking', ''),
        timestamp=d.get('timestamp', 0),
    )
