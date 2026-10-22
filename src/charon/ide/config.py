"""Operator-owned IDE configuration; never discover executable commands in repos.

The optional file is <state_dir>/ide.json. Runtime-supplied ToolContext metadata
wins over the file. Nothing is installed, executed, or written by this loader.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

MAX_CONFIG_BYTES = 64 * 1024
KEYS = ('ide_lsp_servers', 'ide_debug_adapters')


def configured_context(ctx):
    """Return a context with explicit operator configuration, or raise ValueError.

    Services still validate individual definitions before starting a process.
    A missing file means auto-discovery for LSP and no configured debugger.
    """
    metadata = dict(ctx.metadata or {})
    if ctx.state_dir is None or all(key in metadata for key in KEYS):
        return ctx
    path = Path(ctx.state_dir) / 'ide.json'
    if not path.exists():
        return ctx
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'IDE configuration must be a regular non-symlink file: {path}')
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError('IDE configuration exceeds 64 KiB')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f'Invalid IDE configuration {path}: {exc}') from exc
    if not isinstance(data, dict):
        raise ValueError('IDE configuration must be an object')
    if 'ide_lsp_servers' in data and not isinstance(data['ide_lsp_servers'], list):
        raise ValueError('ide_lsp_servers must be a list')
    if 'ide_debug_adapters' in data and not isinstance(data['ide_debug_adapters'], dict):
        raise ValueError('ide_debug_adapters must be an object')
    for key in KEYS:
        if key in data:
            metadata.setdefault(key, data[key])
    return replace(ctx, metadata=metadata)
