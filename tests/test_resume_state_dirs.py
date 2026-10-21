"""/resume reads the install's state, says where it looked when empty, and can
see conversations recorded under another checkout of the same install."""
from __future__ import annotations

from pathlib import Path

from backend import commands_core, common
from chat_backend import ChatBackend
from charon.conversation.conversation_store import save_conversation
from charon.infra import config


def _backend(state_dir, monkeypatch, others=()):
    monkeypatch.setattr(common, 'STATE_DIR', state_dir)
    monkeypatch.setattr(commands_core, '_other_state_dirs', lambda: list(others))
    events: list = []
    monkeypatch.setattr(common, 'emit', lambda e: events.append(e))
    return ChatBackend(), events


def test_default_state_dir_is_the_install_root_not_the_launch_directory(monkeypatch, tmp_path):
    monkeypatch.delenv('CHARON_STATE_DIR', raising=False)
    monkeypatch.chdir(tmp_path)
    root = Path(config.__file__).resolve().parents[3]
    assert config.default_state_dir() == root / '.charon_state'
    assert (root / 'apps' / 'tui' / 'opentui' / 'chat_backend.py').is_file()
    monkeypatch.setenv('CHARON_STATE_DIR', str(tmp_path / 'elsewhere'))
    assert config.default_state_dir() == tmp_path / 'elsewhere'


def test_empty_resume_names_the_directory_it_searched(tmp_path, monkeypatch):
    backend, events = _backend(tmp_path / 'state', monkeypatch)
    backend._cmd_resume('/resume', 'r1')
    message = next(e['message'] for e in events if e.get('type') == 'status')
    assert str(tmp_path / 'state' / 'conversations') in message


def test_other_checkout_conversations_are_listed_after_local_and_resumable(tmp_path, monkeypatch):
    local, other = tmp_path / 'local', tmp_path / 'other'
    save_conversation(local, 'session-here', [{'role': 'user', 'content': 'local work', 'timestamp': 1.0}])
    save_conversation(other, 'session-pomemo', [{'role': 'user', 'content': 'pomemo ios app', 'timestamp': 2.0}])
    backend, events = _backend(local, monkeypatch, others=[('charon-wt', other)])

    backend._cmd_resume('/resume', 'r1')
    picker = next(e for e in events if e.get('type') == 'model_picker')
    assert [m['id'] for m in picker['models']] == ['session-here', 'session-pomemo']
    assert picker['models'][1]['desc'] == '[charon-wt] pomemo ios app'

    events.clear()
    monkeypatch.setattr(backend, '_ensure_engine', lambda: (None, None))
    backend._cmd_resume('/resume session-pomemo', 'r2')
    restored = next(e for e in events if e.get('type') == 'conversation_restored')
    assert restored['messages'][0]['content'] == 'pomemo ios app'
    assert (other / 'conversations' / 'session-pomemo.jsonl').exists()   # source untouched
