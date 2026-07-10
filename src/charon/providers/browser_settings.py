"""Browser visibility settings for Charon.

Manages the persistent default and per-session override for whether
the browser should be shown (headed) or hidden (headless) when agents
use the Browser or X tools.

Persistent default: stored in .charon_state/settings.json
  { "browser_visible": true/false }  — default false (headless)

Per-session override: in-memory, keyed by session/agent id.
  Cleared when the session ends. Values: True, False, or None (unset →
  fall back to persistent default).

Resolution order:
  1. Per-session override (if set this session)
  2. Persistent default (from settings.json)
  3. CHARON_BROWSER_HEADLESS env var (legacy, inverted)
  4. Hardcoded default: headless (not visible)

Slash command support (handled in conversation_engine):
  /browser show [--save]   → headed; --save persists
  /browser hide [--save]   → headless; --save persists
  /browser status          → show current state
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional


_SETTINGS_FILE = 'settings.json'
_KEY = 'browser_visible'

_lock = threading.Lock()

# Per-session overrides: agent_id/session_id → bool | None
_session_overrides: dict[str, Optional[bool]] = {}

# Per-session "already prompted this session" flag
_session_prompted: set[str] = set()


# ── Persistent settings ───────────────────────────────────────────────────────

def _settings_path(state_dir: Path) -> Path:
    return state_dir / _SETTINGS_FILE


def _load_settings(state_dir: Path) -> dict:
    path = _settings_path(state_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_settings(state_dir: Path, data: dict) -> None:
    path = _settings_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Merge with existing to avoid clobbering other keys
    existing = _load_settings(state_dir)
    existing.update(data)
    path.write_text(json.dumps(existing, indent=2))


def get_persistent_default(state_dir: Path | None) -> Optional[bool]:
    """Return the saved persistent browser_visible setting, or None if unset."""
    if not state_dir:
        return None
    settings = _load_settings(state_dir)
    val = settings.get(_KEY)
    if val is None:
        return None
    return bool(val)


def set_persistent_default(state_dir: Path, visible: bool) -> None:
    """Save the persistent browser_visible default."""
    with _lock:
        _save_settings(state_dir, {_KEY: visible})


# ── Per-session override ──────────────────────────────────────────────────────

def get_session_override(session_id: str) -> Optional[bool]:
    """Return the per-session override, or None if not set this session."""
    with _lock:
        return _session_overrides.get(session_id)


def set_session_override(session_id: str, visible: bool) -> None:
    """Set a per-session browser visibility override."""
    with _lock:
        _session_overrides[session_id] = visible
        _session_prompted.add(session_id)


def clear_session_override(session_id: str) -> None:
    with _lock:
        _session_overrides.pop(session_id, None)
        _session_prompted.discard(session_id)


def has_been_prompted(session_id: str) -> bool:
    """Return True if we already asked the user this session."""
    with _lock:
        return session_id in _session_prompted


def mark_prompted(session_id: str) -> None:
    with _lock:
        _session_prompted.add(session_id)


# ── Resolution ────────────────────────────────────────────────────────────────

def should_show_browser(
    session_id: str = '',
    state_dir: Path | None = None,
) -> bool:
    """Resolve whether the browser should be visible (headed).

    Priority:
      1. Per-session override
      2. Persistent default in settings.json
      3. CHARON_BROWSER_HEADLESS env var (inverted: '0' → show)
      4. Default: headless (False)
    """
    # 1. Per-session
    if session_id:
        override = get_session_override(session_id)
        if override is not None:
            return override

    # 2. Persistent default
    if state_dir:
        persistent = get_persistent_default(state_dir)
        if persistent is not None:
            return persistent

    # 3. Env var (legacy)
    env = os.environ.get('CHARON_BROWSER_HEADLESS', '')
    if env:
        return env == '0'

    # 4. Hardcoded default: headless
    return False


def needs_session_prompt(session_id: str, state_dir: Path | None) -> bool:
    """True if we should ask the user whether to show the browser this session.

    We prompt if:
    - No per-session answer yet this session
    - AND no persistent default has been saved
    """
    if not session_id:
        return False
    if has_been_prompted(session_id):
        return False
    if state_dir and get_persistent_default(state_dir) is not None:
        return False
    # Also skip if env var is set explicitly
    if os.environ.get('CHARON_BROWSER_HEADLESS', ''):
        return False
    return True


# ── Status string ─────────────────────────────────────────────────────────────

def status_string(session_id: str = '', state_dir: Path | None = None) -> str:
    session_override = get_session_override(session_id) if session_id else None
    persistent = get_persistent_default(state_dir) if state_dir else None
    env_val = os.environ.get('CHARON_BROWSER_HEADLESS', '')
    resolved = should_show_browser(session_id, state_dir)

    lines = ['**Browser visibility settings**']
    lines.append(f'  Resolved: **{"visible (headed)" if resolved else "hidden (headless)"}**')
    lines.append(f'  Persistent default: {_fmt(persistent)} (settings.json)')
    lines.append(f'  Session override:   {_fmt(session_override)}')
    if env_val:
        lines.append(f'  Env CHARON_BROWSER_HEADLESS={env_val} ({"show" if env_val == "0" else "hide"})')
    lines.append('')
    lines.append('Use `/browser show` or `/browser hide` to change for this session.')
    lines.append('Add `--save` to persist the default.')
    return '\n'.join(lines)


def _fmt(val: Optional[bool]) -> str:
    if val is None:
        return 'not set'
    return 'visible' if val else 'hidden'
