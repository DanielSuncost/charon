"""Tests for shade scope enforcement, conversation search, and queue processing."""
import json

from charon.tools import ToolContext, execute_tool, _check_scope
from charon.tools.search_tool import search_conversations, rebuild_index, execute_search
from charon.infra import store_adapter


def setup_function():
    store_adapter.reset_all()


# ── Shade scope enforcement ─────────────────────────────────────────

def test_scope_allows_within_scope(tmp_path):
    ctx = ToolContext(project_root=tmp_path, scope=['src/auth/'])
    result = _check_scope('Read', {'path': 'src/auth/login.py'}, ctx)
    assert result is None  # allowed


def test_scope_blocks_outside_scope(tmp_path):
    ctx = ToolContext(project_root=tmp_path, scope=['src/auth/'])
    result = _check_scope('Write', {'path': 'src/api/routes.py'}, ctx)
    assert result is not None
    assert 'Scope violation' in result


def test_scope_blocks_edit_outside(tmp_path):
    ctx = ToolContext(project_root=tmp_path, scope=['tests/'])
    result = _check_scope('Edit', {'path': 'src/main.py'}, ctx)
    assert result is not None
    assert 'Scope violation' in result


def test_scope_allows_bash(tmp_path):
    """Bash can't be reliably scoped — allowed but shade is told in prompt."""
    ctx = ToolContext(project_root=tmp_path, scope=['src/'])
    result = _check_scope('Bash', {'command': 'rm -rf /'}, ctx)
    assert result is None  # allowed (can't scope bash)


def test_scope_allows_git(tmp_path):
    """Git operates on whole repo — allowed."""
    ctx = ToolContext(project_root=tmp_path, scope=['src/'])
    result = _check_scope('Git', {'action': 'status'}, ctx)
    assert result is None


def test_no_scope_allows_everything(tmp_path):
    ctx = ToolContext(project_root=tmp_path, scope=None)
    result = _check_scope('Write', {'path': '/etc/passwd'}, ctx)
    assert result is None  # no scope = no restriction


def test_scope_multiple_prefixes(tmp_path):
    ctx = ToolContext(project_root=tmp_path, scope=['src/', 'tests/'])
    # Scope is a WRITE contract: writes outside scope are blocked...
    assert _check_scope('Write', {'path': 'src/auth/login.py'}, ctx) is None
    assert _check_scope('Write', {'path': 'tests/test_auth.py'}, ctx) is None
    assert _check_scope('Write', {'path': 'docs/readme.md'}, ctx) is not None


def test_scope_allows_reads_anywhere(tmp_path):
    """Scope gates modifications, not reads. An implementer must be able to read
    context outside its write-scope (e.g. a frozen checker it optimizes toward) —
    and reads were never actually confined anyway, since Bash bypasses scope."""
    ctx = ToolContext(project_root=tmp_path, scope=['src/'])
    assert _check_scope('Read', {'path': 'src/main.py'}, ctx) is None
    assert _check_scope('Read', {'path': 'docs/readme.md'}, ctx) is None
    assert _check_scope('Read', {'path': 'check.py'}, ctx) is None


def test_scope_blocks_sibling_prefix(tmp_path):
    """Scope 'src' must NOT allow a sibling dir like 'src-evil' that merely
    shares a string prefix (path-component boundary required)."""
    ctx = ToolContext(project_root=tmp_path, scope=['src'])
    assert _check_scope('Read', {'path': 'src/main.py'}, ctx) is None
    blocked = _check_scope('Write', {'path': 'src-evil/payload.py'}, ctx)
    assert blocked is not None
    assert 'Scope violation' in blocked


def test_scope_allows_exact_scope_file(tmp_path):
    """A scope entry naming a single file allows exactly that file."""
    ctx = ToolContext(project_root=tmp_path, scope=['src/main.py'])
    assert _check_scope('Edit', {'path': 'src/main.py'}, ctx) is None
    assert _check_scope('Edit', {'path': 'src/main.py.bak'}, ctx) is not None


def test_frozen_blocks_writes_and_edits(tmp_path):
    """Frozen paths must reject Write/Edit even when no scope is set."""
    ctx = ToolContext(project_root=tmp_path, frozen=['src/core.py', 'config/'])
    assert _check_scope('Write', {'path': 'src/core.py'}, ctx) is not None
    assert 'Frozen-path violation' in _check_scope('Edit', {'path': 'src/core.py'}, ctx)
    assert _check_scope('Edit', {'path': 'config/settings.yaml'}, ctx) is not None
    # Non-frozen modifications are allowed.
    assert _check_scope('Write', {'path': 'src/other.py'}, ctx) is None
    # Reads of frozen files are allowed (frozen = must-not-MODIFY).
    assert _check_scope('Read', {'path': 'src/core.py'}, ctx) is None


def test_frozen_respects_path_boundary(tmp_path):
    """Frozen 'config' must not match a sibling 'config-backup'."""
    ctx = ToolContext(project_root=tmp_path, frozen=['config'])
    assert _check_scope('Write', {'path': 'config/a.txt'}, ctx) is not None
    assert _check_scope('Write', {'path': 'config-backup/a.txt'}, ctx) is None


def test_frozen_overrides_scope(tmp_path):
    """A file inside scope but also frozen must be blocked."""
    ctx = ToolContext(project_root=tmp_path, scope=['src/'], frozen=['src/locked.py'])
    assert _check_scope('Edit', {'path': 'src/free.py'}, ctx) is None
    assert _check_scope('Edit', {'path': 'src/locked.py'}, ctx) is not None


def test_scope_enforced_in_execute_tool(tmp_path):
    """execute_tool should block scoped writes."""
    (tmp_path / 'src' / 'auth').mkdir(parents=True)
    ctx = ToolContext(project_root=tmp_path, scope=['src/auth/'])
    # This should be blocked — outside scope
    result = execute_tool('Write', {'path': 'src/api/hack.py', 'content': 'hacked'}, ctx)
    assert result.is_error
    assert 'Scope violation' in result.content
    # Verify file was NOT created
    assert not (tmp_path / 'src' / 'api' / 'hack.py').exists()


def test_scope_allows_in_execute_tool(tmp_path):
    """execute_tool should allow writes within scope."""
    (tmp_path / 'src' / 'auth').mkdir(parents=True)
    ctx = ToolContext(project_root=tmp_path, scope=['src/auth/'])
    result = execute_tool('Write', {'path': 'src/auth/new.py', 'content': 'ok'}, ctx)
    assert not result.is_error
    assert (tmp_path / 'src' / 'auth' / 'new.py').exists()


# ── Conversation search (FTS5) ──────────────────────────────────────

def test_rebuild_index(tmp_path):
    state_dir = tmp_path / 'state'
    conv_dir = state_dir / 'conversations'
    conv_dir.mkdir(parents=True)

    # Write some test conversations
    with (conv_dir / 'AG-001.jsonl').open('w') as f:
        f.write(json.dumps({'role': 'user', 'content': 'Fix the authentication bug in login.py', 'timestamp': 1000}) + '\n')
        f.write(json.dumps({'role': 'assistant', 'content': 'I found the issue in the token validation.', 'timestamp': 1001}) + '\n')

    with (conv_dir / 'AG-002.jsonl').open('w') as f:
        f.write(json.dumps({'role': 'user', 'content': 'Add rate limiting to the API endpoints', 'timestamp': 2000}) + '\n')
        f.write(json.dumps({'role': 'assistant', 'content': 'I will implement a token bucket rate limiter.', 'timestamp': 2001}) + '\n')

    count = rebuild_index(state_dir)
    assert count == 4


def test_search_finds_results(tmp_path):
    state_dir = tmp_path / 'state'
    conv_dir = state_dir / 'conversations'
    conv_dir.mkdir(parents=True)

    with (conv_dir / 'AG-001.jsonl').open('w') as f:
        f.write(json.dumps({'role': 'user', 'content': 'Fix the authentication bug in login.py', 'timestamp': 1000}) + '\n')
        f.write(json.dumps({'role': 'assistant', 'content': 'I fixed the token validation issue.', 'timestamp': 1001}) + '\n')

    rebuild_index(state_dir)
    results = search_conversations(state_dir, 'authentication')
    assert len(results) >= 1
    assert any('authentication' in r.get('content', '').lower() or 'authentication' in r.get('snippet', '').lower() for r in results)


def test_search_no_results(tmp_path):
    state_dir = tmp_path / 'state'
    conv_dir = state_dir / 'conversations'
    conv_dir.mkdir(parents=True)

    with (conv_dir / 'AG-001.jsonl').open('w') as f:
        f.write(json.dumps({'role': 'user', 'content': 'Hello world', 'timestamp': 1000}) + '\n')

    rebuild_index(state_dir)
    results = search_conversations(state_dir, 'quantum entanglement')
    assert len(results) == 0


def test_search_filter_by_agent(tmp_path):
    state_dir = tmp_path / 'state'
    conv_dir = state_dir / 'conversations'
    conv_dir.mkdir(parents=True)

    with (conv_dir / 'AG-001.jsonl').open('w') as f:
        f.write(json.dumps({'role': 'user', 'content': 'Fix the authentication module', 'timestamp': 1000}) + '\n')

    with (conv_dir / 'AG-002.jsonl').open('w') as f:
        f.write(json.dumps({'role': 'user', 'content': 'Fix the authentication gateway', 'timestamp': 2000}) + '\n')

    rebuild_index(state_dir)

    all_results = search_conversations(state_dir, 'authentication')
    assert len(all_results) == 2

    filtered = search_conversations(state_dir, 'authentication', agent_id='AG-001')
    assert len(filtered) == 1


def test_search_tool(tmp_path):
    state_dir = tmp_path / 'state'
    conv_dir = state_dir / 'conversations'
    conv_dir.mkdir(parents=True)

    with (conv_dir / 'AG-001.jsonl').open('w') as f:
        f.write(json.dumps({'role': 'user', 'content': 'Refactor the database connection pooling', 'timestamp': 1000}) + '\n')

    rebuild_index(state_dir)
    ctx = ToolContext(project_root=tmp_path, state_dir=state_dir)
    result = execute_search({'query': 'database connection'}, ctx)
    assert not result.is_error
    assert 'database' in result.content.lower() or 'connection' in result.content.lower()


def test_search_empty_query(tmp_path):
    ctx = ToolContext(project_root=tmp_path, state_dir=tmp_path / 'state')
    result = execute_search({'query': ''}, ctx)
    assert result.is_error


def test_search_auto_rebuilds_on_first_use(tmp_path):
    """First search should automatically build the index."""
    state_dir = tmp_path / 'state'
    conv_dir = state_dir / 'conversations'
    conv_dir.mkdir(parents=True)

    with (conv_dir / 'AG-001.jsonl').open('w') as f:
        f.write(json.dumps({'role': 'user', 'content': 'Deploy the application to staging', 'timestamp': 1000}) + '\n')

    # Don't call rebuild_index — search should do it automatically
    results = search_conversations(state_dir, 'deploy staging')
    assert len(results) >= 1
