"""Tests for compaction with file tracking."""

from charon.conversation.conversation_engine import _extract_file_ops, _format_file_ops
from charon.providers import Message, ToolCall


# ── File operation extraction ───────────────────────────────────────

def test_extract_read_ops():
    messages = [
        Message(role='assistant', content='Let me check', tool_calls=[
            ToolCall(id='tc-1', name='Read', arguments={'path': 'src/auth/login.py'}),
            ToolCall(id='tc-2', name='Read', arguments={'path': 'src/auth/register.py'}),
        ]),
        Message(role='tool_result', content='file content', tool_call_id='tc-1'),
        Message(role='tool_result', content='file content', tool_call_id='tc-2'),
    ]
    read, written, edited = _extract_file_ops(messages)
    assert 'src/auth/login.py' in read
    assert 'src/auth/register.py' in read
    assert len(written) == 0
    assert len(edited) == 0


def test_extract_write_ops():
    messages = [
        Message(role='assistant', content='Creating file', tool_calls=[
            ToolCall(id='tc-1', name='Write', arguments={'path': 'src/new_module.py', 'content': 'x'}),
        ]),
    ]
    read, written, edited = _extract_file_ops(messages)
    assert 'src/new_module.py' in written


def test_extract_edit_ops():
    messages = [
        Message(role='assistant', content='Editing', tool_calls=[
            ToolCall(id='tc-1', name='Edit', arguments={'path': 'src/main.py', 'oldText': 'a', 'newText': 'b'}),
        ]),
    ]
    read, written, edited = _extract_file_ops(messages)
    assert 'src/main.py' in edited


def test_read_only_excludes_modified():
    """Files that were read AND modified should only appear in modified."""
    messages = [
        Message(role='assistant', content='Read then edit', tool_calls=[
            ToolCall(id='tc-1', name='Read', arguments={'path': 'src/fix.py'}),
            ToolCall(id='tc-2', name='Edit', arguments={'path': 'src/fix.py', 'oldText': 'a', 'newText': 'b'}),
        ]),
    ]
    read, written, edited = _extract_file_ops(messages)
    assert 'src/fix.py' not in read  # read-only excludes edited files
    assert 'src/fix.py' in edited


def test_extract_mixed_ops():
    messages = [
        Message(role='assistant', content='Working', tool_calls=[
            ToolCall(id='tc-1', name='Read', arguments={'path': 'README.md'}),
            ToolCall(id='tc-2', name='Read', arguments={'path': 'src/api.py'}),
            ToolCall(id='tc-3', name='Edit', arguments={'path': 'src/api.py'}),
            ToolCall(id='tc-4', name='Write', arguments={'path': 'tests/test_api.py', 'content': 'x'}),
            ToolCall(id='tc-5', name='Bash', arguments={'command': 'pytest tests/'}),
        ]),
    ]
    read, written, edited = _extract_file_ops(messages)
    assert 'README.md' in read
    assert 'src/api.py' not in read  # was also edited
    assert 'src/api.py' in edited
    assert 'tests/test_api.py' in written


def test_extract_empty():
    read, written, edited = _extract_file_ops([])
    assert read == []
    assert written == []
    assert edited == []


def test_extract_no_tool_calls():
    messages = [
        Message(role='user', content='hello'),
        Message(role='assistant', content='hi'),
    ]
    read, written, edited = _extract_file_ops(messages)
    assert read == []


# ── File ops formatting ─────────────────────────────────────────────

def test_format_file_ops():
    text = _format_file_ops(
        files_read=['README.md', 'docs/plan.md'],
        files_written=['tests/test_new.py'],
        files_edited=['src/main.py', 'src/api.py'],
    )
    assert 'Files read: README.md, docs/plan.md' in text
    assert 'Files edited: src/main.py, src/api.py' in text
    assert 'Files created: tests/test_new.py' in text


def test_format_file_ops_empty():
    text = _format_file_ops([], [], [])
    assert text == ''


def test_format_file_ops_truncates():
    many_files = [f'src/file_{i}.py' for i in range(20)]
    text = _format_file_ops(many_files, [], [])
    assert '... and 5 more' in text
