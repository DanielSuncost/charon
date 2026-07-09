"""Tests for intelligent task summarization."""

from task_summarizer import summarize_fast, _short_path, _extract_command_highlight


# ── Path shortening ─────────────────────────────────────────────────

def test_short_path_already_short():
    assert _short_path('file.py') == 'file.py'
    assert _short_path('src/file.py') == 'src/file.py'


def test_short_path_long():
    assert _short_path('apps/core-daemon/tools/memory_tools.py') == 'tools/memory_tools.py'
    assert _short_path('/home/user/project/src/auth/login.py') == 'auth/login.py'


# ── Command highlight extraction ────────────────────────────────────

def test_extract_pytest_result():
    output = """
collecting ... collected 25 items

test_foo.py::test_one PASSED
test_foo.py::test_two PASSED
========================= 25 passed in 0.21s =========================
"""
    result = _extract_command_highlight('pytest tests/', output)
    assert 'passed' in result


def test_extract_build_result():
    output = """
Bundled 18 modules in 29ms
  index.js  0.85 MB (entry point)
"""
    result = _extract_command_highlight('bun build src/index.ts', output)
    assert 'Bundled' in result


def test_extract_git_result():
    output = "[main abc1234] Fix auth bug\n 2 files changed, 15 insertions(+), 3 deletions(-)"
    result = _extract_command_highlight('git commit -m "Fix auth bug"', output)
    assert 'Fix auth bug' in result or 'files changed' in result


def test_extract_empty():
    assert _extract_command_highlight('echo hi', '') == ''


# ── Fast summarization ──────────────────────────────────────────────

def test_summarize_edit_task():
    summary = summarize_fast(
        instruction='Fix the auth bug in login.py',
        tool_calls=[
            {'tool': 'Read', 'arguments': {'path': 'src/auth/login.py'}, 'is_error': False},
            {'tool': 'Edit', 'arguments': {'path': 'src/auth/login.py', 'oldText': 'x', 'newText': 'y'}, 'is_error': False},
            {'tool': 'Bash', 'arguments': {'command': 'pytest tests/auth/'}, 'is_error': False,
             'result': '5 passed in 0.3s'},
        ],
        response_text='I fixed the bug by changing x to y.',
        errors=[],
        total_turns=3,
    )
    assert 'auth/login.py' in summary
    assert 'edited' in summary
    assert 'passed' in summary


def test_summarize_write_task():
    summary = summarize_fast(
        instruction='Create a new config file',
        tool_calls=[
            {'tool': 'Write', 'arguments': {'path': 'config/settings.json', 'content': '{}'}, 'is_error': False},
        ],
        response_text='Created the config.',
        errors=[],
        total_turns=1,
    )
    assert 'wrote' in summary
    assert 'settings.json' in summary


def test_summarize_read_only():
    summary = summarize_fast(
        instruction='What does the auth module do?',
        tool_calls=[
            {'tool': 'Read', 'arguments': {'path': 'src/auth/login.py'}, 'is_error': False},
            {'tool': 'Read', 'arguments': {'path': 'src/auth/register.py'}, 'is_error': False},
        ],
        response_text='The auth module handles user login and registration.',
        errors=[],
        total_turns=2,
    )
    assert 'read' in summary
    assert 'auth' in summary


def test_summarize_command_task():
    summary = summarize_fast(
        instruction='Run the tests',
        tool_calls=[
            {'tool': 'Bash', 'arguments': {'command': 'python -m pytest tests/ -q'}, 'is_error': False,
             'result': '291 passed in 5.01s'},
        ],
        response_text='All tests pass.',
        errors=[],
        total_turns=1,
    )
    assert 'command' in summary or 'ran' in summary
    assert 'passed' in summary


def test_summarize_with_errors():
    summary = summarize_fast(
        instruction='Deploy to staging',
        tool_calls=[
            {'tool': 'Bash', 'arguments': {'command': 'make deploy'}, 'is_error': True,
             'result': 'Connection refused'},
        ],
        response_text='',
        errors=['Connection refused to staging server'],
        total_turns=1,
    )
    assert 'error' in summary.lower()


def test_summarize_no_tools():
    summary = summarize_fast(
        instruction='Explain how the auth system works',
        tool_calls=[],
        response_text='The auth system uses JWT tokens stored in httpOnly cookies...',
        errors=[],
        total_turns=1,
    )
    # Should fall back to response text
    assert 'JWT' in summary or 'auth' in summary


def test_summarize_empty():
    summary = summarize_fast(
        instruction='',
        tool_calls=[],
        response_text='',
        errors=[],
        total_turns=1,
    )
    assert 'Completed' in summary


def test_summarize_multiple_edits():
    summary = summarize_fast(
        instruction='Refactor the API module',
        tool_calls=[
            {'tool': 'Edit', 'arguments': {'path': 'src/api/routes.py'}, 'is_error': False},
            {'tool': 'Edit', 'arguments': {'path': 'src/api/handlers.py'}, 'is_error': False},
            {'tool': 'Edit', 'arguments': {'path': 'src/api/middleware.py'}, 'is_error': False},
            {'tool': 'Write', 'arguments': {'path': 'src/api/utils.py', 'content': '...'}, 'is_error': False},
            {'tool': 'Bash', 'arguments': {'command': 'pytest'}, 'is_error': False,
             'result': '42 passed in 2.1s'},
        ],
        response_text='Refactored the API module.',
        errors=[],
        total_turns=4,
    )
    assert 'edited' in summary
    assert 'wrote' in summary
    assert 'routes.py' in summary or 'api/' in summary
