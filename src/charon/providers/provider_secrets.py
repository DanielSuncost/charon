"""Read provider credentials without putting secret values on command lines."""
from __future__ import annotations

import os
from pathlib import Path


def read_secret_env(value_name: str, file_name: str) -> str:
    """Return a direct environment secret, then a file-backed secret.

    File-backed credentials are useful in containers: the host can mount a
    mode-0600 file read-only and expose only its path in the environment.
    Errors deliberately name the path variable, never the secret value.
    """
    direct = str(os.environ.get(value_name) or '').strip()
    if direct:
        return direct

    raw_path = str(os.environ.get(file_name) or '').strip()
    if not raw_path:
        return ''
    try:
        return Path(raw_path).read_text(encoding='utf-8').strip()
    except OSError as exc:
        raise ValueError(f'cannot read credential file from {file_name}: {exc}') from exc
