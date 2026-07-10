"""Shared bootstrap and low-level helpers for the chat backend package.

Holds the sys.path bootstrap for src/ (the charon package) plus the module-level
state (ROOT, STATE_DIR) and the emit/_load_json primitives. Other backend
modules access mutable/patchable state via attribute lookup (common.emit,
common.STATE_DIR) so tests can monkeypatch it in one place.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

# Suppress noisy library output that would corrupt the JSON protocol
os.environ['TRANSFORMERS_VERBOSITY'] = 'error'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'src'))

STATE_DIR = ROOT / '.charon_state'

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


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
    except Exception as e:
        _diag('common', 'state JSON file unreadable; using default value', error=e, file=path.name)
        return default
