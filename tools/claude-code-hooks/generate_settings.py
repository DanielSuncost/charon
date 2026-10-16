"""Generate Claude Code hook settings for Charon integration.

Produces a hooks block for .claude/settings.local.json that points every
relevant hook event at charon_hook_bridge.py.
"""
from __future__ import annotations

import json
from pathlib import Path

_BRIDGE = Path(__file__).resolve().parent / 'charon_hook_bridge.py'

HOOK_EVENTS = [
    'SessionStart',
    'UserPromptSubmit',
    'PreToolUse',
    'PostToolUse',
    'Stop',
]


def generate_hooks_block(bridge_path: Path | str | None = None) -> dict:
    """Return the hooks dict for .claude/settings.local.json."""
    bridge = str(bridge_path or _BRIDGE)
    hooks = {}
    for event in HOOK_EVENTS:
        slug = event[0].lower() + event[1:]  # SessionStart -> sessionStart... nope, keep kebab
        # Claude Code hook commands receive the event name as a subcommand.
        slug = ''.join(
            f'-{c.lower()}' if c.isupper() and i > 0 else c.lower()
            for i, c in enumerate(event)
        )
        hooks[event] = [{'type': 'command', 'command': f'{bridge} {slug}'}]
    return hooks


def generate_settings(bridge_path: Path | str | None = None) -> dict:
    """Return full settings dict ready for .claude/settings.local.json."""
    return {'hooks': generate_hooks_block(bridge_path)}


def merge_into_settings(existing: dict, bridge_path: Path | str | None = None) -> dict:
    """Merge Charon hooks into an existing settings dict without clobbering."""
    result = dict(existing)
    new_hooks = generate_hooks_block(bridge_path)
    current_hooks = result.get('hooks') or {}
    if not isinstance(current_hooks, dict):
        current_hooks = {}
    for event, entries in new_hooks.items():
        existing_entries = current_hooks.get(event) or []
        if not isinstance(existing_entries, list):
            existing_entries = []
        # Avoid duplicates: skip if a charon_hook_bridge command is already present.
        bridge_str = str(bridge_path or _BRIDGE)
        already = any(bridge_str in str(e.get('command', '')) for e in existing_entries)
        if not already:
            existing_entries.extend(entries)
        current_hooks[event] = existing_entries
    result['hooks'] = current_hooks
    return result


def write_settings(project_dir: Path, bridge_path: Path | str | None = None) -> Path:
    """Write or merge hooks into project_dir/.claude/settings.local.json."""
    claude_dir = project_dir / '.claude'
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / 'settings.local.json'
    existing = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except Exception:
            existing = {}
    merged = merge_into_settings(existing, bridge_path)
    settings_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + '\n')
    return settings_path


if __name__ == '__main__':
    print(json.dumps(generate_settings(), indent=2))
