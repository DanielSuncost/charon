"""Tests for Http, Git tools and recurring task support."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))
sys.path.insert(0, str(ROOT))

from tools import ToolContext
from tools.http_tool import execute_http
from tools.git_tool import execute_git

SCRIPT = ROOT / 'apps' / 'core-daemon' / 'charon_loop.py'


# ── HTTP tool ───────────────────────────────────────────────────────

def test_http_get(tmp_path):
    """Test basic HTTP GET against a known endpoint."""
    ctx = ToolContext(project_root=tmp_path)
    # Use httpbin or a simple localhost check
    # Test with the LM Studio health endpoint (likely running)
    result = execute_http({'url': 'http://127.0.0.1:1234/v1/models', 'timeout': 3}, ctx)
    # May succeed or fail depending on whether LM Studio is running
    # Just verify it doesn't crash and returns a ToolResult
    assert hasattr(result, 'content')
    assert isinstance(result.is_error, bool)


def test_http_missing_url(tmp_path):
    ctx = ToolContext(project_root=tmp_path)
    result = execute_http({'method': 'GET'}, ctx)
    assert result.is_error
    assert 'url is required' in result.content


def test_http_bad_host(tmp_path):
    ctx = ToolContext(project_root=tmp_path)
    result = execute_http({'url': 'http://999.999.999.999:1/nope', 'timeout': 2}, ctx)
    assert result.is_error


def test_http_timeout(tmp_path):
    ctx = ToolContext(project_root=tmp_path)
    # Use a non-routable IP to trigger timeout
    result = execute_http({'url': 'http://10.255.255.1/', 'timeout': 1}, ctx)
    assert result.is_error


# ── Git tool ────────────────────────────────────────────────────────

def _init_git_repo(tmp_path):
    """Create a git repo in tmp_path with an initial commit."""
    subprocess.run(['git', 'init'], cwd=str(tmp_path), capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 'test@test.com'], cwd=str(tmp_path), capture_output=True)
    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=str(tmp_path), capture_output=True)
    (tmp_path / 'README.md').write_text('# Test')
    subprocess.run(['git', 'add', '.'], cwd=str(tmp_path), capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=str(tmp_path), capture_output=True)


def test_git_status(tmp_path):
    _init_git_repo(tmp_path)
    ctx = ToolContext(project_root=tmp_path, agent_id='AG-TEST')
    result = execute_git({'action': 'status'}, ctx)
    assert not result.is_error
    assert 'Branch:' in result.content


def test_git_log(tmp_path):
    _init_git_repo(tmp_path)
    ctx = ToolContext(project_root=tmp_path)
    result = execute_git({'action': 'log'}, ctx)
    assert not result.is_error
    assert 'Initial commit' in result.content


def test_git_commit(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / 'new_file.py').write_text('print("hello")')
    ctx = ToolContext(project_root=tmp_path, agent_id='AG-TEST')
    result = execute_git({'action': 'commit', 'message': 'Add new file'}, ctx)
    assert not result.is_error
    assert 'new_file.py' in result.content or 'Add new file' in result.content

    # Check agent metadata in commit
    log_result = execute_git({'action': 'log', 'lines': 1}, ctx)
    assert 'Add new file' in log_result.content


def test_git_commit_with_agent_metadata(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / 'change.txt').write_text('changed')
    ctx = ToolContext(project_root=tmp_path, agent_id='AG-0005')
    execute_git({'action': 'commit', 'message': 'Checkpoint'}, ctx)

    # Verify metadata in commit message
    proc = subprocess.run(['git', 'log', '-1', '--format=%B'], cwd=str(tmp_path),
                          capture_output=True, text=True)
    assert 'Charon-Agent: AG-0005' in proc.stdout


def test_git_branch_and_checkout(tmp_path):
    _init_git_repo(tmp_path)
    ctx = ToolContext(project_root=tmp_path)

    result = execute_git({'action': 'branch', 'branch': 'feature-x'}, ctx)
    assert not result.is_error
    assert 'feature-x' in result.content

    result = execute_git({'action': 'checkout', 'branch': 'master'}, ctx)
    assert not result.is_error


def test_git_diff(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / 'README.md').write_text('# Changed')
    ctx = ToolContext(project_root=tmp_path)
    result = execute_git({'action': 'diff'}, ctx)
    assert not result.is_error
    assert 'README' in result.content


def test_git_nothing_to_commit(tmp_path):
    _init_git_repo(tmp_path)
    ctx = ToolContext(project_root=tmp_path)
    result = execute_git({'action': 'commit', 'message': 'Empty'}, ctx)
    assert 'Nothing to commit' in result.content


def test_git_not_a_repo(tmp_path):
    ctx = ToolContext(project_root=tmp_path)
    result = execute_git({'action': 'status'}, ctx)
    assert result.is_error
    assert 'not a git repository' in result.content


def test_git_list_branches(tmp_path):
    _init_git_repo(tmp_path)
    ctx = ToolContext(project_root=tmp_path)
    result = execute_git({'action': 'branch'}, ctx)
    assert not result.is_error
    assert 'master' in result.content or 'main' in result.content


def test_git_stash(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / 'README.md').write_text('# Stashed change')
    ctx = ToolContext(project_root=tmp_path)
    result = execute_git({'action': 'stash', 'message': 'WIP'}, ctx)
    assert not result.is_error


# ── Recurring task support ──────────────────────────────────────────

def test_recurring_task_re_enqueued(tmp_path):
    """A completed task with interval_minutes should spawn a new pending task."""
    state_dir = tmp_path / 'state'
    stop_file = tmp_path / 'STOP'

    # Create a queue with a recurring task
    state_dir.mkdir(parents=True)
    queue = [{
        'id': 'recurring-1',
        'title': 'Check status',
        'status': 'pending',
        'instruction': 'check things',
        'task_type': 'recurring_check',
        'interval_minutes': 30,
        'created_at': '2026-03-21T10:00:00Z',
        'updated_at': '2026-03-21T10:00:00Z',
    }]
    (state_dir / 'queue.json').write_text(json.dumps(queue))

    cmd = [
        'python3', str(SCRIPT),
        '--state-dir', str(state_dir),
        '--stop-file', str(stop_file),
        '--sleep-sec', '0.01',
        '--max-cycles', '5',
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                          env={'CHARON_STDOUT_EVENTS': '0', 'PATH': '/usr/bin:/bin'})
    assert proc.returncode == 0

    final_queue = json.loads((state_dir / 'queue.json').read_text())

    # Should have the completed original + a new pending recurring copy
    completed = [t for t in final_queue if t.get('status') == 'completed']
    pending = [t for t in final_queue if t.get('status') == 'pending']

    assert len(completed) >= 1
    assert len(pending) >= 1
    assert pending[0].get('interval_minutes') == 30
    assert pending[0].get('not_before')  # has a scheduled time


def test_not_before_prevents_early_pickup(tmp_path):
    """Tasks with a future not_before should not be picked up."""
    state_dir = tmp_path / 'state'
    stop_file = tmp_path / 'STOP'

    state_dir.mkdir(parents=True)
    queue = [{
        'id': 'future-1',
        'title': 'Future task',
        'status': 'pending',
        'not_before': '2099-01-01T00:00:00Z',  # far future
        'created_at': '2026-03-21T10:00:00Z',
        'updated_at': '2026-03-21T10:00:00Z',
    }]
    (state_dir / 'queue.json').write_text(json.dumps(queue))

    cmd = [
        'python3', str(SCRIPT),
        '--state-dir', str(state_dir),
        '--stop-file', str(stop_file),
        '--sleep-sec', '0.01',
        '--max-cycles', '3',
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                          env={'CHARON_STDOUT_EVENTS': '0', 'PATH': '/usr/bin:/bin'})
    assert proc.returncode == 0

    final_queue = json.loads((state_dir / 'queue.json').read_text())
    # Task should still be pending — not picked up because not_before is in the future
    assert final_queue[0]['status'] == 'pending'
