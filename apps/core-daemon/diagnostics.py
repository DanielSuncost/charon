"""Lightweight, TUI-safe diagnostics for otherwise-silent degradation.

Many subsystems swallow exceptions by design (best-effort), which hides real
degradation: e.g. if sqlite-vec fails to load, recall silently falls back to
FTS-only; if an OAuth refresh fails, the only signal is a generic error much
later. This records such events to an append-only JSONL file under the state
dir so they are observable after the fact.

It deliberately NEVER writes to stdout/stderr — those streams carry the TUI
backend protocol and the daemon's structured logs, so spewing to them would
corrupt the UI. It also never raises: diagnostics must not break their caller.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

_FILENAME = 'diagnostics.jsonl'


def _resolve_state_dir(state_dir=None) -> Path | None:
    if state_dir:
        return Path(state_dir).expanduser()
    raw = os.environ.get('CHARON_STATE_DIR')
    if raw:
        return Path(raw).expanduser()
    default = Path.home() / '.charon_state'
    return default if default.exists() else None


def record(component: str, message: str, *, state_dir=None,
           error: BaseException | str | None = None, **fields) -> None:
    """Append one diagnostic record. Never raises; never touches stdout/stderr.

    `component` is a short subsystem tag (e.g. 'memory_engine'), `message` a
    human-readable summary, `error` the swallowed exception (or its text), and
    `fields` any extra structured context.
    """
    try:
        sd = _resolve_state_dir(state_dir)
        if not sd:
            return
        entry = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'component': str(component),
            'message': str(message),
        }
        if error is not None:
            if isinstance(error, BaseException):
                entry['error'] = f'{type(error).__name__}: {error}'
            else:
                entry['error'] = str(error)
        for k, v in fields.items():
            entry[k] = v
        sd.mkdir(parents=True, exist_ok=True)
        with open(sd / _FILENAME, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception:
        pass  # diagnostics must never break the caller


def read_recent(state_dir, limit: int = 50) -> list[dict]:
    """Return the most recent diagnostic records (newest last)."""
    try:
        sd = _resolve_state_dir(state_dir)
        if not sd:
            return []
        path = sd / _FILENAME
        if not path.exists():
            return []
        lines = path.read_text(encoding='utf-8').splitlines()
        out = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception:
        return []


__all__ = ['record', 'read_recent']
