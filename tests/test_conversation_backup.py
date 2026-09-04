from __future__ import annotations

from charon.conversation.conversation_store import (
    load_conversation,
    save_conversation,
    sync_conversation_backup,
)


def test_backup_appends_only_new_tail(tmp_path):
    messages = [
        {'role': 'user', 'content': 'one', 'timestamp': 1},
        {'role': 'assistant', 'content': 'two', 'timestamp': 2},
    ]
    assert sync_conversation_backup(tmp_path, 'agent', messages) == 2
    path = tmp_path / 'conversations' / 'agent.jsonl'
    inode = path.stat().st_ino
    original = path.read_bytes()

    messages.append({'role': 'user', 'content': 'three', 'timestamp': 3})
    assert sync_conversation_backup(tmp_path, 'agent', messages) == 1

    assert path.stat().st_ino == inode
    assert path.read_bytes().startswith(original)
    assert load_conversation(tmp_path, 'agent') == messages


def test_backup_repairs_a_non_prefix_transcript(tmp_path):
    save_conversation(
        tmp_path,
        'agent',
        [{'role': 'user', 'content': 'stale', 'timestamp': 1}],
    )
    expected = [
        {'role': 'user', 'content': 'current', 'timestamp': 1},
        {'role': 'assistant', 'content': 'answer', 'timestamp': 2},
    ]

    assert sync_conversation_backup(tmp_path, 'agent', expected) == 2
    assert load_conversation(tmp_path, 'agent') == expected


def test_backup_detects_external_truncation(tmp_path):
    messages = [{'role': 'user', 'content': 'one', 'timestamp': 1}]
    sync_conversation_backup(tmp_path, 'agent', messages)
    path = tmp_path / 'conversations' / 'agent.jsonl'
    path.write_text('', encoding='utf-8')

    messages.append({'role': 'assistant', 'content': 'two', 'timestamp': 2})
    assert sync_conversation_backup(tmp_path, 'agent', messages) == 2
    assert load_conversation(tmp_path, 'agent') == messages
