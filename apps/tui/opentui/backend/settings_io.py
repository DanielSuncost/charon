"""UI settings, project registry, message store, hermes runtime-home IO."""
from __future__ import annotations

import json
import re
import yaml
from pathlib import Path

from backend import common
from backend.textutils import _iso_to_epoch


def _hermes_conversation_runtime_dir(room_id: str, participant_name: str) -> Path:
    rid = re.sub(r'[^a-zA-Z0-9._-]+', '-', str(room_id or '').strip()).strip('-_.') or 'room'
    pname = re.sub(r'[^a-zA-Z0-9._-]+', '-', str(participant_name or '').strip()).strip('-_.') or 'participant'
    return common.STATE_DIR / 'hermes-conversation-runtime' / rid / pname


def _write_hermes_runtime_home(home: Path, *, model: str, base_url: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    config = {
        'model': {
            'provider': 'custom',
            'base_url': str(base_url or 'http://127.0.0.1:1234/v1').rstrip('/'),
            'default': str(model or 'qwen3-30b-a3b').strip(),
        },
        'toolsets': ['all'],
        'agent': {'max_turns': 60, 'verbose': False, 'reasoning_effort': 'medium'},
        'display': {'compact': False, 'personality': 'helpful'},
        'terminal': {'backend': 'local', 'cwd': str(common.ROOT), 'timeout': 180},
    }
    (home / 'config.yaml').write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding='utf-8')
    (home / '.env').write_text(
        '\n'.join([
            f'OPENAI_BASE_URL={str(base_url or "http://127.0.0.1:1234/v1").rstrip("/")}',
            'OPENAI_API_KEY=no-key-required',
            f'LLM_MODEL={str(model or "qwen3-30b-a3b").strip()}',
            '',
        ]),
        encoding='utf-8',
    )


def _full_messages_from_store(agent_id: str) -> list | None:
    """Load full raw message list from the lossless SQLite store.

    Returns a list of Message objects, or None if unavailable.
    Used by save paths to ensure JSONL always contains the complete
    history (not compacted engine.messages).
    """
    try:
        from charon.context.context_store import ContextStore
        from charon.infra.store_adapter import get_db
        from charon.providers import Message as _Msg
        db = get_db(common.STATE_DIR)
        stored = ContextStore.get_messages_for_agent(db, agent_id, limit=10000)
        if not stored:
            return None
        return [
            _Msg(role=sm.role, content=sm.content,
                 tool_calls=sm.tool_calls,
                 tool_call_id=sm.tool_call_id,
                 tool_name=sm.tool_name,
                 is_error=sm.is_error,
                 thinking=sm.thinking,
                 timestamp=_iso_to_epoch(sm.created_at))
            for sm in stored
        ]
    except Exception:
        return None


def _ui_settings_path() -> Path:
    return common.STATE_DIR / 'ui_settings.json'


def _load_ui_settings() -> dict:
    return common._load_json(_ui_settings_path(), {}) or {}


def _save_ui_settings(settings: dict) -> None:
    path = _ui_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False))


def _projects_registry_path() -> Path:
    return common.STATE_DIR / 'projects.json'


def _load_project_registry() -> list[dict]:
    data = common._load_json(_projects_registry_path(), [])
    return data if isinstance(data, list) else []


def _save_project_registry(projects: list[dict]) -> None:
    path = _projects_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(projects, indent=2, ensure_ascii=False))


def _project_slug(text: str) -> str:
    import re
    slug = re.sub(r'[^a-zA-Z0-9._-]+', '-', str(text or '').strip()).strip('-_.').lower()
    return slug[:96] or 'project'
