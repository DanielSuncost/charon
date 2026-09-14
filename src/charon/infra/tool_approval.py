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

import json
import re
import threading
from pathlib import Path
from typing import Any

from charon.infra import config


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

# Tools that access the network. Some of these were historically treated as
# safe even though they perform HTTP/browser I/O; keeping one complete registry
# makes the approval policy consistent.
NETWORK_TOOLS = {'Http', 'Web', 'Paper', 'SourceDiscovery', 'Browser'}

RESEARCH_SOURCE_APPROVAL_VALUES = {'auto', 'ask'}
DEFAULT_APPROVAL_CONFIG = {
    'research_sources': 'auto',
}
APPROVAL_CONFIG_FILENAME = 'approval_config.json'

_config_lock = threading.Lock()


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

    if tool_name == 'X':
        action = str(params.get('action', '')).strip().lower()
        if action in ('open_login', 'login_status', 'fetch_post', 'fetch_bookmarks', 'fetch_new_bookmarks', 'triage_new_bookmarks'):
            target = params.get('url', action)
            return 'network', f'X:{action}: {str(target)[:60]}'
        if action in ('deep_dive_bookmark', 'save_investigation', 'mark_presented', 'enqueue_investigation', 'capture_idea', 'schedule_bookmarks_review'):
            return 'write', f'X:{action}'
        return 'safe', ''

    if tool_name == 'SpawnBatch':
        tasks = params.get('tasks', [])
        return 'write', f'spawn {len(tasks)} shade workers'

    if tool_name == 'SpawnShade':
        return 'write', 'spawn shade worker'

    if tool_name == 'PyKernel':
        action = str(params.get('action', 'run')).strip().lower()
        if action == 'run':
            return 'dangerous', 'persistent code execution kernel (arbitrary Python, can spawn shades)'
        return 'safe', ''

    if tool_name == 'Refine':
        action = str(params.get('action', '')).strip().lower()
        if action == 'propose':
            skill = str(params.get('skill', '')).strip()
            return 'write', f'judged self-refinement of skill: {skill}'
        return 'safe', ''

    return 'safe', ''


def is_read_only_research_source_call(tool_name: str, params: dict) -> bool:
    """Return whether a call only reads/discovers research sources.

    This is deliberately action-aware. In particular, a GET is a source read
    while an HTTP POST is not, and browser navigation/inspection is distinct
    from clicking or typing into a page.
    """
    if tool_name in {'Paper', 'SourceDiscovery', 'Web'}:
        return True

    if tool_name == 'Http':
        return str(params.get('method') or 'GET').strip().upper() in {'GET', 'HEAD'}

    if tool_name == 'Browser':
        action = str(params.get('action') or '').strip().lower()
        return action in {
            'navigate', 'screenshot', 'scroll', 'go_back', 'wait',
            'get_state', 'assert_text', 'assert_selector', 'vision',
        }

    if tool_name == 'X':
        action = str(params.get('action') or '').strip().lower()
        return action in {
            'login_status', 'fetch_post', 'fetch_bookmarks',
            'fetch_new_bookmarks', 'triage_new_bookmarks',
        }

    return False


# ── Persisted policy ────────────────────────────────────────────────

def _approval_config_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / APPROVAL_CONFIG_FILENAME


def load_approval_config(state_dir: Path | str | None) -> dict[str, Any]:
    """Load approval configuration, filling safe defaults."""
    loaded: dict[str, Any] = {}
    if state_dir is not None:
        path = _approval_config_path(state_dir)
        with _config_lock:
            try:
                value = json.loads(path.read_text()) if path.exists() else {}
                if isinstance(value, dict):
                    loaded = value
            except (OSError, json.JSONDecodeError, TypeError):
                loaded = {}

    result = dict(DEFAULT_APPROVAL_CONFIG)
    configured = str(loaded.get('research_sources') or '').strip().lower()
    if configured in RESEARCH_SOURCE_APPROVAL_VALUES:
        result['research_sources'] = configured
    return result


def save_approval_config(state_dir: Path | str, approval_config: dict[str, Any]) -> dict[str, Any]:
    """Validate and atomically persist approval configuration."""
    policy = str(approval_config.get('research_sources') or '').strip().lower()
    if policy not in RESEARCH_SOURCE_APPROVAL_VALUES:
        raise ValueError('research source approval policy must be "auto" or "ask"')

    normalized = {'research_sources': policy}
    path = _approval_config_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp')
    with _config_lock:
        tmp.write_text(json.dumps(normalized, indent=2) + '\n')
        tmp.replace(path)
    return normalized


def configured_research_source_approval_policy(state_dir: Path | str | None) -> str:
    """Return the persisted research-source policy without env overrides."""
    return str(load_approval_config(state_dir)['research_sources'])


def get_research_source_approval_policy(state_dir: Path | str | None = None) -> str:
    """Return the effective policy, with the environment taking precedence."""
    return (
        config.research_source_approval_override()
        or configured_research_source_approval_policy(state_dir)
    )


def set_research_source_approval_policy(
    state_dir: Path | str,
    policy: str,
) -> dict[str, Any]:
    """Persist ``auto`` or ``ask`` and return configured/effective values."""
    normalized = save_approval_config(
        state_dir,
        {'research_sources': str(policy).strip().lower()},
    )
    return {
        **normalized,
        'effective_research_sources': get_research_source_approval_policy(state_dir),
        'env_override': config.research_source_approval_override(),
    }


# ── Approval state ──────────────────────────────────────────────────

_lock = threading.Lock()
_session_approved: dict[str, set[str]] = {}
_permanent_approved: set[str] = set()
_pending_approvals: dict[str, dict] = {}


def is_approval_skipped() -> bool:
    """Check if approval is globally disabled."""
    return config.skip_approval()


def needs_approval(
    tool_name: str,
    params: dict,
    *,
    session_id: str = 'default',
    approval_mode: str = 'normal',
    state_dir: Path | str | None = None,
    operation_domain: str = '',
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

    # Background Libris/research agents are expected to gather evidence
    # autonomously. Only read-only source access is covered; dangerous commands,
    # HTTP mutations, and interactive browser actions continue through the
    # ordinary approval policy.
    if (
        str(operation_domain).strip().lower() == 'research'
        and risk == 'network'
        and is_read_only_research_source_call(tool_name, params)
        and get_research_source_approval_policy(state_dir) == 'auto'
    ):
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


def get_approval_status(
    session_id: str = 'default',
    *,
    state_dir: Path | str | None = None,
) -> dict:
    """Get current approval status for display."""
    with _lock:
        return {
            'skip_all': is_approval_skipped(),
            'session_approved': sorted(_session_approved.get(session_id, set())),
            'permanent_approved': sorted(_permanent_approved),
            'research_sources': get_research_source_approval_policy(state_dir),
            'configured_research_sources': configured_research_source_approval_policy(state_dir),
            'research_sources_env_override': config.research_source_approval_override(),
        }


def request_attached_browser_approval(params: dict, ctx) -> bool:
    """Per-call privilege approval: no research, scope or remembered-tool bypass.

    This gate runs inside the serialized executor to prevent session switches
    between classification and use, and protects direct executor invocations.
    Explicit global approval-off remains the operator's override.
    """
    if is_approval_skipped():
        return True
    from charon.tools import _request_interactive_approval
    approved, _ = _request_interactive_approval(
        'Browser', params, 'dangerous',
        'attach to or change a logged-in browser session', ctx,
    )
    return approved
