"""Durable JSON state-file IO.

Two hazards this module removes:

1. Read-then-rewrite data loss. The common pattern
   ``try: json.loads(path.read_text()) except Exception: return default``
   silently converts a *transient* parse/IO error into permanent data loss
   the moment the caller rewrites the file with the empty default. Here,
   an unreadable-but-existing file is first preserved by renaming it to
   ``<name>.corrupt-<n>`` so a later write can never destroy the original.

2. Torn writes. ``path.write_text(...)`` can leave a half-written file on
   crash or disk-full. ``write_json_atomic`` writes to a temp file in the
   same directory and ``os.replace``s it into place.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


def quarantine_corrupt_file(path: Path, *, component: str, error: BaseException | str | None = None) -> Path | None:
    """Rename an unreadable state file to ``<name>.corrupt-<n>`` so a later
    write cannot destroy it. Returns the quarantine path, or None on failure.
    """
    path = Path(path)
    target = path.with_name(f'{path.name}.corrupt-{os.getpid()}')
    for i in range(1000):
        candidate = path.with_name(f'{path.name}.corrupt-{i}')
        if not candidate.exists():
            target = candidate
            break
    try:
        os.replace(path, target)
    except Exception as e:
        _diag(component, f'{path.name} unreadable AND could not be quarantined; a later write may destroy it', error=e, original_error=str(error))
        return None
    _diag(component, f'{path.name} unreadable; original preserved as {target.name}', error=error, quarantined_to=str(target))
    return target


def read_json_or_quarantine(path: Path, default: Any, *, component: str) -> Any:
    """Read JSON from ``path``. Missing file returns ``default``. An
    unreadable/corrupt file is quarantined (see ``quarantine_corrupt_file``),
    recorded to diagnostics, and ``default`` is returned.
    """
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        quarantine_corrupt_file(path, component=component, error=e)
        return default


def write_json_atomic(path: Path, data: Any, *, indent: int | None = 2, **json_kwargs: Any) -> None:
    """Write ``data`` as JSON to ``path`` atomically: temp file in the same
    directory, then ``os.replace``. Extra kwargs pass through to json.dumps.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(json.dumps(data, indent=indent, **json_kwargs))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
