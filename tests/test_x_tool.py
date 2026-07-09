"""Tests for the X tool's workflow helpers and bookmark investigation index."""
import json

from tools import ToolContext
from tools.x_tool import execute_x
import tools.x_tool as x_tool
from conversation_runtime import load_queue
from goal_runtime import list_goals
from tool_approval import classify_tool_risk


def _ctx(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(parents=True, exist_ok=True)
    return ToolContext(project_root=tmp_path, state_dir=state, agent_id='AG-X')


def test_capture_idea_saves_backlog_goal(tmp_path):
    ctx = _ctx(tmp_path)

    result = execute_x({
        'action': 'capture_idea',
        'project': 'charon',
        'text': 'Investigate a browser-backed X ingestion workflow',
    }, ctx)

    assert not result.is_error
    goals = list_goals(ctx.state_dir, project='charon', status='backlog')
    assert any('browser-backed X ingestion workflow' in g.get('title', '') for g in goals)


def test_enqueue_investigation_creates_agent_task(tmp_path):
    ctx = _ctx(tmp_path)

    result = execute_x({
        'action': 'enqueue_investigation',
        'project': 'charon',
        'url': 'https://x.com/someone/status/123',
        'text': 'Check if this should become a feature.',
    }, ctx)

    assert not result.is_error
    payload = json.loads(result.content)
    task = payload['queued_task']
    assert task['task_type'] == 'agent_task'
    assert task['owner_agent_id'] == 'AG-X'
    assert 'Source URL: https://x.com/someone/status/123' in task['instruction']

    queue = load_queue(ctx.state_dir)
    assert any(t.get('id') == task['id'] for t in queue)


def test_schedule_bookmarks_review_creates_recurring_task(tmp_path):
    ctx = _ctx(tmp_path)

    result = execute_x({
        'action': 'schedule_bookmarks_review',
        'project': 'charon',
        'interval_minutes': 45,
        'max_items_per_run': 7,
    }, ctx)

    assert not result.is_error
    payload = json.loads(result.content)
    task = payload['scheduled_task']
    assert task['interval_minutes'] == 45
    assert task['task_type'] == 'agent_task'
    assert 'fetch_new_bookmarks' in task['instruction']
    assert 'save_investigation' in task['instruction']


def test_save_list_get_and_search_investigations(tmp_path):
    ctx = _ctx(tmp_path)

    save = execute_x({
        'action': 'save_investigation',
        'url': 'https://x.com/someone/status/123',
        'project': 'charon',
        'title': 'Interesting post',
        'summary': 'Browser-backed X access looks best.',
        'report': 'Detailed report about using a persistent Playwright profile for bookmarks.',
        'recommendation': 'implement',
    }, ctx)
    assert not save.is_error
    saved = json.loads(save.content)
    assert saved['bookmark_id'] == 'someone-status-123'
    assert saved['investigated_at']

    listed = execute_x({'action': 'list_investigations', 'new_only': True}, ctx)
    payload = json.loads(listed.content)
    assert payload['count'] == 1
    assert payload['items'][0]['bookmark_id'] == 'someone-status-123'
    assert payload['items'][0]['recommendation'] == 'implement'
    assert payload['items'][0]['selection_index'] == 1

    got = execute_x({'action': 'get_investigation', 'bookmark_id': 'someone-status-123'}, ctx)
    full = json.loads(got.content)
    assert 'persistent Playwright profile' in full['investigation_report']

    found = execute_x({'action': 'search_investigations', 'query': 'Playwright'}, ctx)
    found_payload = json.loads(found.content)
    assert found_payload['count'] == 1
    assert found_payload['items'][0]['bookmark_id'] == 'someone-status-123'


def test_mark_presented_removes_item_from_new_list(tmp_path):
    ctx = _ctx(tmp_path)

    execute_x({
        'action': 'save_investigation',
        'url': 'https://x.com/someone/status/123',
        'project': 'charon',
        'summary': 'Browser-backed X access looks best.',
    }, ctx)

    mark = execute_x({'action': 'mark_presented', 'bookmark_id': 'someone-status-123'}, ctx)
    mark_payload = json.loads(mark.content)
    assert mark_payload['count'] == 1

    listed = execute_x({'action': 'list_investigations', 'new_only': True}, ctx)
    payload = json.loads(listed.content)
    assert payload['count'] == 0


def test_triage_new_bookmarks_returns_quick_summaries(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)

    async def fake_fetch(_ctx, limit, *, new_only=False):
        assert new_only is True
        bookmarks = [
            {
                'bookmark_id': 'someone-status-123',
                'status_url': 'https://x.com/someone/status/123',
                'author': 'someone',
                'text': 'Interesting browser automation idea',
                'links': ['https://example.com/a'],
            },
            {
                'bookmark_id': 'other-status-456',
                'status_url': 'https://x.com/other/status/456',
                'author': 'other',
                'text': 'Another post',
                'links': [],
            },
        ]
        for b in bookmarks:
            x_tool._upsert_seen_bookmark(_ctx, b, source='bookmarks')
        return {
            'url': 'https://x.com/i/bookmarks',
            'count': 2,
            'bookmarks': bookmarks,
        }

    def fake_triage(_ctx, bookmarks, project, max_concurrent=3):
        assert len(bookmarks) == 2
        x_tool._record_triage(_ctx, bookmark_id='someone-status-123', url='https://x.com/someone/status/123', summary='Useful X/browser automation idea.', relevance='high')
        x_tool._record_triage(_ctx, bookmark_id='other-status-456', url='https://x.com/other/status/456', summary='Probably lower priority.', relevance='low')
        return {
            'batch': {'id': 'batch-123'},
            'triaged': [
                {
                    'bookmark_id': 'someone-status-123',
                    'status': 'completed',
                    'summary': 'Useful X/browser automation idea.',
                    'relevance': 'high',
                    'status_url': 'https://x.com/someone/status/123',
                    'author': 'someone',
                    'text': 'Interesting browser automation idea',
                },
                {
                    'bookmark_id': 'other-status-456',
                    'status': 'completed',
                    'summary': 'Probably lower priority.',
                    'relevance': 'low',
                    'status_url': 'https://x.com/other/status/456',
                    'author': 'other',
                    'text': 'Another post',
                },
            ],
        }

    monkeypatch.setattr(x_tool, '_fetch_bookmarks_impl', fake_fetch)
    monkeypatch.setattr(x_tool, '_run_triage_shades', fake_triage)

    result = execute_x({'action': 'triage_new_bookmarks', 'project': 'charon', 'limit': 5}, ctx)
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload['count'] == 2
    assert payload['items'][0]['bookmark_id'] == 'someone-status-123'
    assert payload['items'][0]['relevance'] == 'high'


def test_deep_dive_bookmark_enqueues_task_from_saved_record(tmp_path):
    ctx = _ctx(tmp_path)

    execute_x({
        'action': 'save_investigation',
        'url': 'https://x.com/someone/status/123',
        'project': 'charon',
        'summary': 'Quick note',
    }, ctx)
    x_tool._record_triage(ctx, bookmark_id='someone-status-123', url='https://x.com/someone/status/123', summary='This one seems promising.', relevance='high')

    listed = execute_x({'action': 'list_investigations', 'new_only': True}, ctx)
    listed_payload = json.loads(listed.content)
    assert listed_payload['items'][0]['selection_index'] == 1

    result = execute_x({'action': 'deep_dive_bookmark', 'selection_index': 1, 'project': 'charon'}, ctx)
    assert not result.is_error
    payload = json.loads(result.content)
    task = payload['queued_task']
    assert task['task_type'] == 'agent_task'
    assert 'Stored bookmark ID: someone-status-123' in task['instruction']
    assert 'Existing triage summary: This one seems promising.' in task['instruction']

    got = execute_x({'action': 'get_investigation', 'selection_index': 1}, ctx)
    assert not got.is_error


def test_x_tool_risk_classification():
    risk, _ = classify_tool_risk('X', {'action': 'fetch_post', 'url': 'https://x.com/a/status/1'})
    assert risk == 'network'

    risk, _ = classify_tool_risk('X', {'action': 'triage_new_bookmarks'})
    assert risk == 'network'

    risk, _ = classify_tool_risk('X', {'action': 'save_investigation', 'url': 'https://x.com/a/status/1', 'summary': 'idea'})
    assert risk == 'write'

    risk, _ = classify_tool_risk('X', {'action': 'search_investigations', 'query': 'idea'})
    assert risk == 'safe'
