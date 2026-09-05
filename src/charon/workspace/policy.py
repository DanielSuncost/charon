"""Overseer send/spawn policy — the single decision for "may the overseer put input into
this session?" (port of Acheron's ``src/overseer/policy.js``; same codes and messages).
"""
from __future__ import annotations

import math
import re
from typing import Any

SECRET_PATTERNS = [
    re.compile(r'sk-ant-[A-Za-z0-9_-]{16,}'),
    re.compile(r'sk-[A-Za-z0-9_-]{16,}'),
    re.compile(r'Bearer\s+[A-Za-z0-9._~+/=-]{16,}'),
    re.compile(r'AKIA[0-9A-Z]{16}'),
    re.compile(r'ghp_[A-Za-z0-9]{20,}'),
    re.compile(r'gho_[A-Za-z0-9]{20,}'),
    re.compile(r'xox[baprs]-[A-Za-z0-9-]{10,}'),
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----'),
]

BUSY_WINDOW_MS = 10_000


def redact(text: Any) -> str:
    out = '' if text is None else str(text)
    for pattern in SECRET_PATTERNS:
        out = pattern.sub(lambda m: m.group(0)[:6] + '…[redacted]', out)
    return out


def looks_like_approval(text: Any) -> bool:
    """"y", "yes", "n", "1", Enter… — the shape of an answer to a permission prompt."""
    t = ('' if text is None else str(text)).strip().lower()
    return len(t) <= 5 and re.fullmatch(r'(y|yes|n|no|a|ok|\d)?', t) is not None


def _deny(code: str, reason: str) -> dict[str, Any]:
    return {'ok': False, 'code': code, 'reason': reason}


def evaluate_send_policy(*, action: str, paused: bool = False, is_self: bool = False, wired: bool = False,
                         target_status: str | None = None, user_typed_ago_ms: float | None = None, content: str = '',
                         force: bool = False, forward_approvals: bool = False, callsign: str = '') -> dict[str, Any]:
    """Facts in, verdict out: ``{'ok': True}`` or ``{'ok': False, 'code', 'reason'}``.

    action: dispatch | intervene | interrupt | label.  ``user_typed_ago_ms`` is ms since the user
    last typed in the target (None / inf when never).
    """
    name = callsign or 'target'
    if paused:
        return _deny('paused', 'overseer is PAUSED by the user — reads only')
    if is_self:
        return _deny('self', 'cannot target the overseer itself')
    if not wired:
        return _deny('write_requires_wire', f'{name} is not wired to the overseer (the user wires sessions with the Wire button)')
    if action in ('label', 'interrupt'):
        return {'ok': True}
    if target_status == 'error' and not force:
        return _deny('no_error_targets', f'{name} is in error state (force:true to override, logged)')
    if target_status == 'detached':
        return _deny('no_detached_targets', f'{name} has no live session')
    ago = math.inf if user_typed_ago_ms is None else user_typed_ago_ms
    if ago < BUSY_WINDOW_MS:
        return _deny('busy', f'the user is typing in {name} — retry next cycle')
    if target_status == 'waiting' and not forward_approvals and looks_like_approval(content):
        return _deny('forward_approvals', 'sending an approval into a waiting session is disabled — ask the user (acheron_ask_user)')
    return {'ok': True}


def evaluate_spawn_policy(*, policy: str, session_count: int, max_blocks: int) -> dict[str, Any]:
    if policy == 'deny':
        return _deny('spawn_denied', 'spawning is disabled in Settings → Overseer')
    if session_count >= max_blocks:
        return _deny('spawn_cap', f'workspace already has {session_count} sessions (cap {max_blocks})')
    if policy == 'confirm':
        return {'ok': True, 'confirm': True}
    return {'ok': True, 'confirm': False}
