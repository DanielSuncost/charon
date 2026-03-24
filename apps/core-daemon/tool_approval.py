"""Tool approval — dangerous operation detection and user confirmation.

Intercepts tool calls before execution and checks for dangerous patterns.
If a dangerous operation is detected, the tool call is blocked and the
agent is told to ask the user for permission.

Three approval levels:
  - session: approved for this session only (default)
  - permanent: always approved (stored in config)
  - skip: all checks disabled (CHARON_SKIP_APPROVAL=1)

Dangerous patterns cover:
  - Destructive filesystem operations (rm -rf, chmod 777, mkfs)
  - System modifications (systemctl stop, chown root)
  - SQL destructive operations (DROP, DELETE without WHERE, TRUNCATE)
  - Remote code execution (curl | sh, python -c)
  - Sensitive file access (.env, credentials, ssh keys)
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any


# ── Dangerous patterns ──────────────────────────────────────────────

DANGEROUS_PATTERNS = [
    # Filesystem destruction
    (r'\brm\s+(-[^\s]*\s+)*/', 'delete in root path'),
    (r'\brm\s+-[^\s]*r', 'recursive delete'),
    (r'\brm\s+--recursive\b', 'recursive delete'),
    (r'\bchmod\s+(-[^\s]*\s+)*777\b', 'world-writable permissions'),
    (r'\bmkfs\b', 'format filesystem'),
    (r'\bdd\s+.*if=', 'disk copy'),
    (r'>\s*/dev/sd', 'write to block device'),
    (r'>\s*/etc/', 'overwrite system config'),

    # Process/service control
    (r'\bsystemctl\s+(stop|disable|mask)\b', 'stop/disable system service'),
    (r'\bkill\s+-9\s+-1\b', 'kill all processes'),
    (r'\bpkill\s+-9\b', 'force kill processes'),

    # SQL destruction
    (r'\bDROP\s+(TABLE|DATABASE)\b', 'SQL DROP'),
    (r'\bDELETE\s+FROM\b(?!.*\bWHERE\b)', 'SQL DELETE without WHERE'),
    (r'\bTRUNCATE\s+(TABLE)?\s*\w', 'SQL TRUNCATE'),

    # Remote code execution
    (r'\b(curl|wget)\b.*\|\s*(ba)?sh\b', 'pipe remote content to shell'),
    (r':()\s*{\s*:\s*\|\s*:&\s*}\s*;:', 'fork bomb'),

    # Sensitive file access
    (r'\bcat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc)', 'read secrets file'),
    (r'authorized_keys', 'SSH key modification'),
]

# Tools that modify the filesystem
WRITE_TOOLS = {'Write', 'Edit', 'Git'}

# Tools that access the network
NETWORK_TOOLS = {'Http', 'Web'}


# ── Detection ───────────────────────────────────────────────────────

def detect_dangerous_command(command: str) -> tuple[bool, str | None, str | None]:
    """Check if a bash command matches dangerous patterns.

    Returns (is_dangerous, pattern_key, description).
    """
    for pattern, description in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE | re.DOTALL):
            # Extract a short key for approval tracking
            key = re.sub(r'[^a-z_]', '', description.replace(' ', '_'))[:30]
            return True, key, description
    return False, None, None


def classify_tool_risk(tool_name: str, params: dict) -> tuple[str, str]:
    """Classify a tool call's risk level.

    Returns (risk_level, reason).
    risk_level: 'safe', 'write', 'network', 'dangerous'
    """
    if tool_name == 'Bash':
        command = str(params.get('command', ''))
        is_dangerous, _, desc = detect_dangerous_command(command)
        if is_dangerous:
            return 'dangerous', desc or 'dangerous command'
        # Any bash command that modifies files
        if any(k in command for k in ('rm ', 'mv ', 'cp ', 'chmod ', 'chown ', 'mkdir ', 'touch ')):
            return 'write', f'filesystem modification: {command[:60]}'
        return 'safe', ''

    if tool_name in WRITE_TOOLS:
        path = params.get('path', '')
        return 'write', f'{tool_name} on {path}'

    if tool_name in NETWORK_TOOLS:
        url = params.get('url', params.get('query', ''))
        return 'network', f'{tool_name}: {url[:60]}'

    if tool_name == 'SpawnBatch':
        tasks = params.get('tasks', [])
        return 'write', f'spawn {len(tasks)} shade workers'

    if tool_name == 'SpawnShade':
        return 'write', f'spawn shade worker'

    return 'safe', ''


# ── Approval state ──────────────────────────────────────────────────

_lock = threading.Lock()
_session_approved: dict[str, set[str]] = {}
_permanent_approved: set[str] = set()
_pending_approvals: dict[str, dict] = {}


def is_approval_skipped() -> bool:
    """Check if approval is globally disabled."""
    return os.environ.get('CHARON_SKIP_APPROVAL', '0') in ('1', 'true', 'yes')


def needs_approval(
    tool_name: str,
    params: dict,
    *,
    session_id: str = 'default',
    approval_mode: str = 'normal',
) -> tuple[bool, str, str]:
    """Check if a tool call needs user approval.

    approval_mode:
      'normal' — ask for dangerous + network, auto-approve writes
      'strict' — ask for everything except reads
      'off' — never ask (same as CHARON_SKIP_APPROVAL)

    Returns (needs_approval, risk_level, reason).
    """
    if approval_mode == 'off' or is_approval_skipped():
        return False, 'safe', ''

    risk, reason = classify_tool_risk(tool_name, params)

    if risk == 'safe':
        return False, risk, reason

    if risk == 'dangerous':
        # Always ask for dangerous, check if already approved
        key = f'dangerous:{reason}'
        with _lock:
            if key in _permanent_approved:
                return False, risk, reason
            if key in _session_approved.get(session_id, set()):
                return False, risk, reason
        return True, risk, reason

    if risk == 'network' and approval_mode in ('normal', 'strict'):
        key = f'network:{tool_name}'
        with _lock:
            if key in _permanent_approved:
                return False, risk, reason
            if key in _session_approved.get(session_id, set()):
                return False, risk, reason
        return True, risk, reason

    if risk == 'write' and approval_mode == 'strict':
        key = f'write:{tool_name}'
        with _lock:
            if key in _permanent_approved:
                return False, risk, reason
            if key in _session_approved.get(session_id, set()):
                return False, risk, reason
        return True, risk, reason

    return False, risk, reason


def approve_for_session(session_id: str, approval_key: str) -> None:
    """Approve an operation for this session."""
    with _lock:
        _session_approved.setdefault(session_id, set()).add(approval_key)


def approve_permanently(approval_key: str) -> None:
    """Approve an operation permanently."""
    with _lock:
        _permanent_approved.add(approval_key)


def approve_tool_for_session(session_id: str, tool_name: str) -> None:
    """Approve all calls to a specific tool for this session."""
    with _lock:
        s = _session_approved.setdefault(session_id, set())
        s.add(f'network:{tool_name}')
        s.add(f'write:{tool_name}')
        s.add(f'dangerous:{tool_name}')


def clear_session_approvals(session_id: str) -> None:
    """Clear all session-specific approvals."""
    with _lock:
        _session_approved.pop(session_id, None)


def get_approval_status(session_id: str = 'default') -> dict:
    """Get current approval status for display."""
    with _lock:
        return {
            'skip_all': is_approval_skipped(),
            'session_approved': sorted(_session_approved.get(session_id, set())),
            'permanent_approved': sorted(_permanent_approved),
        }
