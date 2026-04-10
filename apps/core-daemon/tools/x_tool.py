"""X (x.com) tool — logged-in post and bookmark access plus workflow helpers.

Why browser-based instead of API-based:
- works with the user's normal x.com session
- can access bookmarks and other logged-in views
- avoids fragile unofficial API wrappers for core use cases

Actions:
  open_login                  Open x.com login in a persistent browser profile.
  login_status                Check whether the persistent x.com profile appears logged in.
  fetch_post                  Open a post URL and extract the main post, visible thread items, and links.
  fetch_bookmarks             Open bookmarks, extract visible bookmarked posts, and record them locally.
  fetch_new_bookmarks         Same as fetch_bookmarks, but returns only bookmarks not yet investigated.
  save_investigation          Persist Charon's research output for a bookmark/post.
  list_investigations         List investigated bookmarks from the local record index.
  get_investigation           Fetch one stored investigation by bookmark_id or URL.
  search_investigations       Full-text-ish search over stored investigation records.
  mark_presented              Mark stored investigations as shown to the user.
  enqueue_investigation       Add an agent_task to investigate a post/link for a project.
  capture_idea                Save an idea to the project's backlog.
  schedule_bookmarks_review   Create a recurring task to review X bookmarks.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools import ToolContext, ToolResult


X_TOOL_DEF = {
    'name': 'X',
    'description': (
        'Access x.com with a persistent logged-in browser profile and bridge interesting posts into Charon workflows. '
        'Can open login, fetch posts/bookmarks, keep a local investigation index, save research outputs, query past bookmark investigations, '
        'enqueue investigations, capture ideas, and schedule recurring bookmark reviews.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': [
                    'open_login', 'login_status', 'fetch_post', 'fetch_bookmarks', 'fetch_new_bookmarks', 'triage_new_bookmarks', 'deep_dive_bookmark',
                    'save_investigation', 'list_investigations', 'get_investigation', 'search_investigations', 'mark_presented',
                    'enqueue_investigation', 'capture_idea', 'schedule_bookmarks_review',
                ],
                'description': 'X tool action.',
            },
            'url': {
                'type': 'string',
                'description': 'x.com post URL for fetch/get/save/enqueue actions.',
            },
            'bookmark_id': {
                'type': 'string',
                'description': 'Stable local bookmark record ID for get/save/mark actions.',
            },
            'selection_index': {
                'type': 'number',
                'description': '1-based index from the most recent bookmark triage/list result, useful for prompts like "deep dive on #2".',
            },
            'bookmark_ids': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'List of bookmark record IDs for mark_presented.',
            },
            'limit': {
                'type': 'number',
                'description': 'Max posts/items to extract or list (default: 5).',
            },
            'project': {
                'type': 'string',
                'description': 'Project name/path for backlog capture, storage metadata, or queued investigation.',
            },
            'title': {
                'type': 'string',
                'description': 'Optional title for backlog idea, stored investigation, or investigation task.',
            },
            'text': {
                'type': 'string',
                'description': 'Optional note/summary/idea text.',
            },
            'summary': {
                'type': 'string',
                'description': 'Short stored investigation summary.',
            },
            'report': {
                'type': 'string',
                'description': 'Detailed stored investigation report.',
            },
            'recommendation': {
                'type': 'string',
                'description': 'One-line recommendation, e.g. implement/add to backlog/defer.',
            },
            'query': {
                'type': 'string',
                'description': 'Search query for search_investigations.',
            },
            'wait_for_completion': {
                'type': 'boolean',
                'description': 'For triage/deep-dive actions: wait for shade work to finish before returning (default: true for triage).',
            },
            'new_only': {
                'type': 'boolean',
                'description': 'For list_investigations: only items not yet presented to the user.',
            },
            'mark_presented': {
                'type': 'boolean',
                'description': 'For list_investigations: also mark returned items as presented.',
            },
            'owner_agent_id': {
                'type': 'string',
                'description': 'Agent that should own queued investigation/review tasks. Defaults to the current agent.',
            },
            'interval_minutes': {
                'type': 'number',
                'description': 'Recurring interval for schedule_bookmarks_review.',
            },
            'max_items_per_run': {
                'type': 'number',
                'description': 'How many bookmarks the recurring review should inspect per run.',
            },
        },
        'required': ['action'],
    },
}


_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_ready = threading.Event()
_pw = None
_x_context = None
_x_page = None


def _browser_thread_main():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _ready.set()
    _loop.run_forever()


def _ensure_thread():
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _ready.clear()
    _thread = threading.Thread(target=_browser_thread_main, daemon=True)
    _thread.start()
    _ready.wait(timeout=5)


def _run(coro, timeout: int = 60):
    _ensure_thread()
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=timeout)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _profile_dir(ctx: ToolContext) -> Path:
    custom = os.environ.get('CHARON_X_PROFILE_DIR', '').strip()
    if custom:
        return Path(custom).expanduser()
    if ctx.state_dir:
        return Path(ctx.state_dir) / 'browser' / 'x'
    return Path.home() / '.charon-x-profile'


def _record_path(ctx: ToolContext) -> Path:
    if not ctx.state_dir:
        raise RuntimeError('state_dir not available')
    return Path(ctx.state_dir) / 'x_bookmarks.json'


def _load_records(ctx: ToolContext) -> dict[str, Any]:
    path = _record_path(ctx)
    if not path.exists():
        return {'version': 1, 'items': []}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and isinstance(data.get('items'), list):
            return data
    except Exception:
        pass
    return {'version': 1, 'items': []}


def _save_records(ctx: ToolContext, data: dict[str, Any]) -> None:
    path = _record_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _store_last_list(ctx: ToolContext, bookmark_ids: list[str], *, kind: str) -> None:
    data = _load_records(ctx)
    data['last_list'] = {
        'kind': kind,
        'bookmark_ids': list(bookmark_ids),
        'updated_at': _now_iso(),
    }
    _save_records(ctx, data)


def _bookmark_id_from_selection(ctx: ToolContext, selection_index: int | None) -> str | None:
    if selection_index is None:
        return None
    idx = int(selection_index)
    if idx <= 0:
        return None
    data = _load_records(ctx)
    last = data.get('last_list') or {}
    ids = list(last.get('bookmark_ids') or [])
    if idx > len(ids):
        return None
    return str(ids[idx - 1]).strip() or None


def _normalize_url(url: str) -> str:
    url = str(url or '').strip()
    if not url:
        return ''
    if url.startswith('http://') or url.startswith('https://'):
        out = url
    elif url.startswith('x.com/') or url.startswith('twitter.com/'):
        out = f'https://{url}'
    else:
        out = url
    out = out.replace('https://twitter.com/', 'https://x.com/')
    out = out.split('#', 1)[0]
    out = out.split('?', 1)[0]
    return out.rstrip('/')


def _record_id_for_url(url: str) -> str:
    normalized = _normalize_url(url)
    m = re.search(r'x\.com/([^/]+)/status/(\d+)', normalized)
    if m:
        user = re.sub(r'[^a-zA-Z0-9_-]+', '-', m.group(1)).strip('-').lower() or 'user'
        return f'{user}-status-{m.group(2)}'
    digest = hashlib.sha1(normalized.encode('utf-8')).hexdigest()[:12]
    return f'x-{digest}'


def _find_item(data: dict[str, Any], *, bookmark_id: str | None = None, url: str | None = None) -> dict[str, Any] | None:
    target_url = _normalize_url(url or '') if url else ''
    for item in data.get('items', []):
        if bookmark_id and item.get('bookmark_id') == bookmark_id:
            return item
        if target_url and _normalize_url(item.get('status_url', '')) == target_url:
            return item
    return None


def _compact_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        'bookmark_id': item.get('bookmark_id'),
        'status_url': item.get('status_url'),
        'author': item.get('author', ''),
        'text': item.get('text', ''),
        'project': item.get('project'),
        'first_seen_at': item.get('first_seen_at'),
        'investigated_at': item.get('investigated_at'),
        'presented_to_user_at': item.get('presented_to_user_at'),
        'recommendation': item.get('recommendation', ''),
        'summary': item.get('investigation_summary', ''),
    }


def _upsert_seen_bookmark(ctx: ToolContext, entry: dict[str, Any], *, source: str) -> dict[str, Any]:
    data = _load_records(ctx)
    status_url = _normalize_url(entry.get('status_url', ''))
    bookmark_id = entry.get('bookmark_id') or (status_url and _record_id_for_url(status_url)) or ''
    if not bookmark_id:
        raise RuntimeError('Cannot record bookmark without bookmark_id or status_url')

    item = _find_item(data, bookmark_id=bookmark_id, url=status_url)
    now = _now_iso()
    if item is None:
        item = {
            'bookmark_id': bookmark_id,
            'status_url': status_url,
            'author': entry.get('author', ''),
            'text': entry.get('text', ''),
            'time': entry.get('time', ''),
            'links': list(entry.get('links') or []),
            'title': entry.get('title', ''),
            'source': source,
            'first_seen_at': now,
            'last_seen_at': now,
            'seen_count': 1,
            'investigated_at': None,
            'presented_to_user_at': None,
        }
        data['items'].append(item)
    else:
        item['last_seen_at'] = now
        item['seen_count'] = int(item.get('seen_count') or 0) + 1
        if entry.get('author'):
            item['author'] = entry.get('author')
        if entry.get('text'):
            item['text'] = entry.get('text')
        if entry.get('time'):
            item['time'] = entry.get('time')
        if entry.get('title'):
            item['title'] = entry.get('title')
        merged_links = list(dict.fromkeys(list(item.get('links') or []) + list(entry.get('links') or [])))
        item['links'] = merged_links
        if source and not item.get('source'):
            item['source'] = source

    _save_records(ctx, data)
    return item


def _record_investigation(
    ctx: ToolContext,
    *,
    bookmark_id: str | None,
    url: str | None,
    title: str,
    project: str,
    summary: str,
    report: str,
    recommendation: str,
    note: str,
) -> dict[str, Any]:
    data = _load_records(ctx)
    normalized_url = _normalize_url(url or '')
    item = _find_item(data, bookmark_id=bookmark_id, url=normalized_url)
    now = _now_iso()

    if item is None:
        rid = bookmark_id or (normalized_url and _record_id_for_url(normalized_url)) or f'x-{hashlib.sha1(now.encode()).hexdigest()[:12]}'
        item = {
            'bookmark_id': rid,
            'status_url': normalized_url,
            'author': '',
            'text': '',
            'time': '',
            'links': [],
            'title': title or '',
            'source': 'manual',
            'first_seen_at': now,
            'last_seen_at': now,
            'seen_count': 1,
            'investigated_at': None,
            'presented_to_user_at': None,
        }
        data['items'].append(item)

    item['investigated_at'] = now
    item['last_seen_at'] = now
    item['title'] = title or item.get('title', '')
    item['project'] = project or item.get('project')
    item['investigation_summary'] = summary or item.get('investigation_summary', '')
    item['investigation_report'] = report or item.get('investigation_report', '')
    item['recommendation'] = recommendation or item.get('recommendation', '')
    item['investigation_note'] = note or item.get('investigation_note', '')

    _save_records(ctx, data)
    return item


def _mark_presented(ctx: ToolContext, *, bookmark_ids: list[str] | None = None, new_only: bool = False) -> list[dict[str, Any]]:
    data = _load_records(ctx)
    now = _now_iso()
    changed = []
    wanted = set(bookmark_ids or [])
    for item in data.get('items', []):
        if not item.get('investigated_at'):
            continue
        if wanted and item.get('bookmark_id') not in wanted:
            continue
        if new_only and item.get('presented_to_user_at'):
            continue
        item['presented_to_user_at'] = now
        changed.append(item)
    if changed:
        _save_records(ctx, data)
    return changed


def _record_triage(ctx: ToolContext, *, bookmark_id: str | None, url: str | None, summary: str, relevance: str = '') -> dict[str, Any]:
    data = _load_records(ctx)
    item = _find_item(data, bookmark_id=bookmark_id, url=_normalize_url(url or '') or None)
    if item is None:
        raise RuntimeError('No bookmark record found for triage save')
    item['triaged_at'] = _now_iso()
    item['triage_summary'] = summary.strip()
    item['triage_relevance'] = relevance.strip()
    _save_records(ctx, data)
    return item


def _build_triage_batch_tasks(bookmarks: list[dict[str, Any]], project: str, limit_links: int = 2) -> list[dict[str, Any]]:
    tasks = []
    for b in bookmarks:
        bid = b.get('bookmark_id', '')
        status_url = _normalize_url(b.get('status_url', ''))
        text = str(b.get('text', '')).strip()
        author = str(b.get('author', '')).strip()
        links = list(b.get('links') or [])[:limit_links]
        link_lines = '\n'.join(f'- {u}' for u in links) if links else '- (no extracted links)'
        instruction = (
            'You are doing a quick first-pass triage of one new X bookmark for Charon.\n'
            f'Bookmark ID: {bid}\n'
            f'Project: {project or "(none specified)"}\n'
            f'Post URL: {status_url}\n'
            f'Author: {author}\n'
            f'Post text:\n{text or "(no extracted text)"}\n\n'
            f'Visible links:\n{link_lines}\n\n'
            'Task:\n'
            '1. Read the post and quickly infer what it is about.\n'
            '2. If there is an obviously important link, inspect at most one or two links briefly.\n'
            '3. Produce a rough 2-4 sentence summary focused on why the bookmark may matter.\n'
            '4. End with one relevance label: high, medium, low, or unclear.\n'
            '5. Do NOT do a full deep dive or implementation. This is just triage.\n\n'
            'Output format:\n'
            'Summary: <short summary>\n'
            'Relevance: <high|medium|low|unclear>'
        )
        tasks.append({
            'title': f'Triage {bid}',
            'instruction': instruction,
            'constraints': ['Keep it short.', 'Do not modify files.', 'Do not implement anything.'],
        })
    return tasks


def _extract_triage_fields(text: str) -> tuple[str, str]:
    raw = str(text or '').strip()
    if not raw:
        return '', ''
    summary = raw
    relevance = ''
    m_sum = re.search(r'(?im)^summary:\s*(.+)$', raw)
    if m_sum:
        summary = m_sum.group(1).strip()
    m_rel = re.search(r'(?im)^relevance:\s*(high|medium|low|unclear)\b', raw)
    if m_rel:
        relevance = m_rel.group(1).strip().lower()
    return summary, relevance


def _run_triage_shades(ctx: ToolContext, bookmarks: list[dict[str, Any]], project: str, max_concurrent: int = 3) -> dict[str, Any]:
    if not ctx.state_dir:
        raise RuntimeError('state_dir not available')
    from batch_orchestrator import create_batch, run_batch_worker, get_batch

    batch = create_batch(
        Path(ctx.state_dir),
        parent_agent_id=ctx.agent_id or 'AG-X',
        project=str(ctx.project_root),
        goal='Triage new X bookmarks',
        tasks=_build_triage_batch_tasks(bookmarks, project),
        max_concurrent=max(1, min(max_concurrent, 8)),
        constraints=['This is triage only, not a deep dive.'],
        phase_name='analysis',
    )
    run_batch_worker(Path(ctx.state_dir), batch['id'], phase_name='analysis')
    finished = get_batch(Path(ctx.state_dir), batch['id']) or batch

    by_id = {b.get('bookmark_id'): b for b in bookmarks}
    triaged = []
    for task in finished.get('tasks', []):
        instruction = str(task.get('instruction', ''))
        m = re.search(r'Bookmark ID:\s*([^\n]+)', instruction)
        bookmark_id = m.group(1).strip() if m else ''
        target = by_id.get(bookmark_id)
        if not target:
            continue
        raw_summary = str(task.get('result_summary') or '').strip()
        summary, relevance = _extract_triage_fields(raw_summary)
        item = _record_triage(
            ctx,
            bookmark_id=bookmark_id,
            url=target.get('status_url', ''),
            summary=summary or raw_summary,
            relevance=relevance,
        )
        triaged.append({
            'bookmark_id': bookmark_id,
            'status': task.get('status'),
            'summary': item.get('triage_summary', ''),
            'relevance': item.get('triage_relevance', ''),
            'status_url': item.get('status_url', ''),
            'author': item.get('author', ''),
            'text': item.get('text', ''),
        })
    return {'batch': finished, 'triaged': triaged}


def _search_items(data: dict[str, Any], query: str) -> list[dict[str, Any]]:
    q = query.strip().lower()
    if not q:
        return []
    out = []
    for item in data.get('items', []):
        hay = '\n'.join([
            str(item.get('bookmark_id', '')),
            str(item.get('status_url', '')),
            str(item.get('author', '')),
            str(item.get('text', '')),
            str(item.get('title', '')),
            str(item.get('project', '')),
            str(item.get('investigation_summary', '')),
            str(item.get('investigation_report', '')),
            str(item.get('recommendation', '')),
            '\n'.join(item.get('links') or []),
        ]).lower()
        if q in hay:
            out.append(item)
    out.sort(key=lambda x: x.get('investigated_at') or '', reverse=True)
    return out


async def _ensure_browser(ctx: ToolContext, *, headless: bool | None = None):
    global _pw, _x_context, _x_page
    if _x_page is not None and not _x_page.is_closed():
        return _x_page

    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        raise RuntimeError(
            'Playwright is required for X browser actions. Install with: uv pip install playwright && playwright install chromium'
        ) from e

    if _pw is None:
        _pw = await async_playwright().start()

    profile_dir = _profile_dir(ctx)
    profile_dir.mkdir(parents=True, exist_ok=True)
    if headless is None:
        try:
            from browser_settings import should_show_browser
            launch_headless = not should_show_browser(
                getattr(ctx, 'agent_id', '') or '',
                getattr(ctx, 'state_dir', None),
            )
        except Exception:
            launch_headless = os.environ.get('CHARON_BROWSER_HEADLESS', '1') != '0'
    else:
        launch_headless = headless

    _x_context = await _pw.chromium.launch_persistent_context(
        str(profile_dir),
        headless=launch_headless,
        args=['--disable-blink-features=AutomationControlled'],
        viewport={'width': 1440, 'height': 1200},
    )
    pages = _x_context.pages
    _x_page = pages[0] if pages else await _x_context.new_page()
    return _x_page


async def _scrape_articles(page, *, limit: int) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 5), 20))
    rows = await page.evaluate(
        r"""(limit) => {
            const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
            const uniq = (xs) => Array.from(new Set(xs.filter(Boolean)));
            const items = [];
            const articles = Array.from(document.querySelectorAll('article')).slice(0, limit);
            for (const article of articles) {
                const textNodes = Array.from(article.querySelectorAll('[data-testid="tweetText"]'));
                const text = clean(textNodes.map(el => el.innerText || '').join('\n'));
                const timeEl = article.querySelector('time');
                const userName = article.querySelector('[data-testid="User-Name"]');
                const links = uniq(Array.from(article.querySelectorAll('a[href]'))
                    .map(a => a.href)
                    .filter(href => href && !href.startsWith('javascript:')));
                const statusLink = links.find(href => /\/(status)\//.test(href)) || '';
                items.push({
                    author: clean(userName ? userName.innerText : ''),
                    text,
                    time: timeEl ? (timeEl.getAttribute('datetime') || '') : '',
                    status_url: statusLink,
                    links,
                });
            }
            return items;
        }""",
        limit,
    )
    for row in rows:
        row['status_url'] = _normalize_url(row.get('status_url', ''))
        row['bookmark_id'] = _record_id_for_url(row['status_url']) if row.get('status_url') else ''
    return rows


async def _login_status_impl(ctx: ToolContext) -> dict[str, Any]:
    page = await _ensure_browser(ctx)
    await page.goto('https://x.com/home', wait_until='domcontentloaded', timeout=30000)
    await page.wait_for_timeout(2500)
    logged_in = await page.locator('[data-testid="AppTabBar_Home_Link"]').count() > 0
    if not logged_in:
        logged_in = 'login' not in page.url and 'x.com/home' in page.url
    return {
        'logged_in': bool(logged_in),
        'url': page.url,
        'profile_dir': str(_profile_dir(ctx)),
    }


async def _fetch_post_impl(ctx: ToolContext, url: str, limit: int) -> dict[str, Any]:
    page = await _ensure_browser(ctx)
    await page.goto(url, wait_until='domcontentloaded', timeout=30000)
    await page.wait_for_timeout(3000)
    title = await page.title()
    items = await _scrape_articles(page, limit=max(2, limit))
    primary = items[0] if items else {}
    if primary:
        primary['title'] = title
        stored = _upsert_seen_bookmark(ctx, primary, source='post')
        primary['record'] = _compact_view(stored)
    return {
        'url': page.url,
        'title': title,
        'primary_post': primary,
        'visible_thread': items[1:limit],
    }


async def _fetch_bookmarks_impl(ctx: ToolContext, limit: int, *, new_only: bool = False) -> dict[str, Any]:
    page = await _ensure_browser(ctx)
    await page.goto('https://x.com/i/bookmarks', wait_until='domcontentloaded', timeout=30000)
    await page.wait_for_timeout(3500)
    items = await _scrape_articles(page, limit=limit)

    recorded = []
    for item in items:
        stored = _upsert_seen_bookmark(ctx, item, source='bookmarks')
        merged = dict(item)
        merged['record'] = _compact_view(stored)
        recorded.append(merged)

    if new_only:
        recorded = [r for r in recorded if not (r.get('record') or {}).get('investigated_at')]

    return {
        'url': page.url,
        'count': len(recorded),
        'bookmarks': recorded,
        'new_only': bool(new_only),
    }


def _default_investigation_instruction(url: str, note: str, project: str, max_items: int = 10) -> str:
    project_line = f'Project: {project}\n' if project else ''
    note_line = f'User note/context: {note}\n' if note else ''
    return (
        'Investigate this X item using the X, Web, Browser, Search, Read, and Git tools as needed.\n'
        f'{project_line}'
        f'Source URL: {url}\n'
        f'{note_line}'
        'Tasks:\n'
        '1. Open the post and extract its main claim, implementation idea, and any linked resources.\n'
        '2. Follow the most relevant links and assess technical relevance.\n'
        '3. Decide one of: implement now, add to backlog for later, or reject for now with reasons.\n'
        '4. If it is promising but not for immediate implementation, capture a concise backlog idea.\n'
        '5. If it is immediately useful, make the change or create a concrete follow-up task.\n'
        f'6. Keep the investigation focused; inspect at most {max_items} linked/related items unless clearly necessary.\n'
        '7. Save the investigation result with X action=save_investigation so it is queryable later.\n'
        'Return a concise recommendation and any concrete actions taken.'
    )


def _require_state_dir(ctx: ToolContext) -> Path:
    if not ctx.state_dir:
        raise RuntimeError('state_dir not available')
    return Path(ctx.state_dir)


def execute_x(params: dict, ctx: ToolContext) -> ToolResult:
    action = str(params.get('action', '')).strip().lower()

    try:
        if action == 'open_login':
            async def _open_login():
                page = await _ensure_browser(ctx, headless=False)
                await page.goto('https://x.com/i/flow/login', wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(1500)
            _run(_open_login())
            return ToolResult(
                content=(
                    'Opened x.com login in a persistent browser profile. '
                    f'Profile dir: {_profile_dir(ctx)}\n'
                    'Complete login in the browser window, then call X with action=login_status or fetch_post/fetch_bookmarks.'
                )
            )

        if action == 'login_status':
            return ToolResult(content=json.dumps(_run(_login_status_impl(ctx)), indent=2))

        if action == 'fetch_post':
            url = _normalize_url(params.get('url', ''))
            if not url:
                return ToolResult(content='Error: url is required for fetch_post.', is_error=True)
            limit = int(params.get('limit') or 5)
            return ToolResult(content=json.dumps(_run(_fetch_post_impl(ctx, url, limit)), indent=2))

        if action == 'fetch_bookmarks':
            limit = int(params.get('limit') or 5)
            return ToolResult(content=json.dumps(_run(_fetch_bookmarks_impl(ctx, limit, new_only=False)), indent=2))

        if action == 'fetch_new_bookmarks':
            limit = int(params.get('limit') or 5)
            return ToolResult(content=json.dumps(_run(_fetch_bookmarks_impl(ctx, limit, new_only=True)), indent=2))

        if action == 'triage_new_bookmarks':
            limit = int(params.get('limit') or 5)
            project = str(params.get('project') or '').strip()
            state_dir = _require_state_dir(ctx)
            try:
                from worker_provider import ensure_worker_provider_or_request_clarification
                provider_status = ensure_worker_provider_or_request_clarification(state_dir, ctx=ctx, purpose='X bookmark triage')
                if not provider_status.get('ok'):
                    payload = {
                        'count': 0,
                        'items': [],
                        'status': 'needs_provider_choice',
                        'reason': provider_status.get('reason') or 'no_provider',
                        'available_providers': provider_status.get('available_providers') or [],
                        'clarification': provider_status.get('clarification') or {},
                        'message': provider_status.get('question') or 'No usable provider is configured for X bookmark triage.',
                    }
                    return ToolResult(content=json.dumps(payload, indent=2), is_error=True, details=payload)
            except Exception:
                pass
            fetched = _run(_fetch_bookmarks_impl(ctx, limit, new_only=True))
            bookmarks = list(fetched.get('bookmarks') or [])
            if not bookmarks:
                return ToolResult(content=json.dumps({'count': 0, 'items': [], 'message': 'No new bookmarks to triage.'}, indent=2))
            result = _run_triage_shades(ctx, bookmarks, project, max_concurrent=min(len(bookmarks), 3))
            items = result.get('triaged', [])
            items.sort(key=lambda x: (x.get('relevance') != 'high', x.get('relevance') != 'medium', x.get('bookmark_id', '')))
            for i, item in enumerate(items, 1):
                item['selection_index'] = i
            _store_last_list(ctx, [str(i.get('bookmark_id') or '') for i in items if i.get('bookmark_id')], kind='triage_new_bookmarks')
            return ToolResult(content=json.dumps({'count': len(items), 'items': items, 'batch_id': result.get('batch', {}).get('id')}, indent=2))

        if action == 'deep_dive_bookmark':
            state_dir = _require_state_dir(ctx)
            from conversation_runtime import enqueue_agent_task

            data = _load_records(ctx)
            bookmark_id = str(params.get('bookmark_id') or '').strip() or None
            if not bookmark_id and params.get('selection_index') is not None:
                bookmark_id = _bookmark_id_from_selection(ctx, int(params.get('selection_index')))
            url = _normalize_url(params.get('url', '')) or None
            item = _find_item(data, bookmark_id=bookmark_id, url=url)
            if not item:
                return ToolResult(content='Error: bookmark_id or url was not found in stored bookmark records.', is_error=True)

            owner_agent_id = str(params.get('owner_agent_id') or ctx.agent_id or '').strip()
            if not owner_agent_id:
                return ToolResult(content='Error: owner_agent_id is required (or current agent context must be set).', is_error=True)

            project = str(params.get('project') or item.get('project') or '').strip() or None
            title = str(params.get('title') or '').strip() or f'Deep dive X bookmark: {item.get("bookmark_id", "")}'
            note = str(params.get('text') or item.get('triage_summary') or '').strip()
            instruction = _default_investigation_instruction(item.get('status_url', ''), note, project or '', max_items=int(params.get('max_items_per_run') or 10))
            instruction += (
                f'\n\nStored bookmark ID: {item.get("bookmark_id", "")}\n'
                f'Existing triage summary: {item.get("triage_summary", "")}\n'
                'When done, save the full result with X action=save_investigation using this bookmark ID.'
            )
            task = enqueue_agent_task(
                state_dir,
                owner_agent_id=owner_agent_id,
                instruction=instruction,
                title=title,
                project=project,
                priority='normal',
            )
            item['deep_dive_requested_at'] = _now_iso()
            item['deep_dive_task_id'] = task.get('id')
            _save_records(ctx, data)
            return ToolResult(content=json.dumps({'queued_task': task, 'bookmark': _compact_view(item)}, indent=2))

        if action == 'save_investigation':
            title = str(params.get('title') or '').strip()
            project = str(params.get('project') or '').strip()
            summary = str(params.get('summary') or params.get('text') or '').strip()
            report = str(params.get('report') or '').strip()
            recommendation = str(params.get('recommendation') or '').strip()
            note = str(params.get('text') or '').strip()
            bookmark_id = str(params.get('bookmark_id') or '').strip() or None
            url = _normalize_url(params.get('url', '')) or None
            if not bookmark_id and not url:
                return ToolResult(content='Error: bookmark_id or url is required for save_investigation.', is_error=True)
            item = _record_investigation(
                ctx,
                bookmark_id=bookmark_id,
                url=url,
                title=title,
                project=project,
                summary=summary,
                report=report,
                recommendation=recommendation,
                note=note,
            )
            return ToolResult(content=json.dumps(item, indent=2))

        if action == 'list_investigations':
            data = _load_records(ctx)
            limit = max(1, min(int(params.get('limit') or 10), 100))
            project = str(params.get('project') or '').strip()
            new_only = bool(params.get('new_only', False))
            items = [i for i in data.get('items', []) if i.get('investigated_at')]
            if project:
                items = [i for i in items if str(i.get('project') or '') == project]
            if new_only:
                items = [i for i in items if not i.get('presented_to_user_at')]
            items.sort(key=lambda x: x.get('investigated_at') or '', reverse=True)
            items = items[:limit]
            if bool(params.get('mark_presented', False)) and items:
                _mark_presented(ctx, bookmark_ids=[i.get('bookmark_id') for i in items if i.get('bookmark_id')])
                for i in items:
                    i['presented_to_user_at'] = _now_iso()
            indexed_items = []
            for idx, i in enumerate(items, 1):
                indexed_items.append(_compact_view(i) | {
                    'selection_index': idx,
                    'triaged_at': i.get('triaged_at'),
                    'triage_summary': i.get('triage_summary', ''),
                    'triage_relevance': i.get('triage_relevance', ''),
                })
            _store_last_list(ctx, [str(i.get('bookmark_id') or '') for i in items if i.get('bookmark_id')], kind='list_investigations')
            payload = {
                'count': len(items),
                'new_only': new_only,
                'items': indexed_items,
            }
            return ToolResult(content=json.dumps(payload, indent=2))

        if action == 'get_investigation':
            data = _load_records(ctx)
            bookmark_id = str(params.get('bookmark_id') or '').strip() or None
            if not bookmark_id and params.get('selection_index') is not None:
                bookmark_id = _bookmark_id_from_selection(ctx, int(params.get('selection_index')))
            url = _normalize_url(params.get('url', '')) or None
            if not bookmark_id and not url:
                return ToolResult(content='Error: bookmark_id, selection_index, or url is required for get_investigation.', is_error=True)
            item = _find_item(data, bookmark_id=bookmark_id, url=url)
            if not item:
                return ToolResult(content='No stored investigation found.', is_error=True)
            return ToolResult(content=json.dumps(item, indent=2))

        if action == 'search_investigations':
            data = _load_records(ctx)
            query = str(params.get('query') or '').strip()
            if not query:
                return ToolResult(content='Error: query is required for search_investigations.', is_error=True)
            limit = max(1, min(int(params.get('limit') or 10), 100))
            matches = [i for i in _search_items(data, query) if i.get('investigated_at')][:limit]
            payload = {
                'query': query,
                'count': len(matches),
                'items': [_compact_view(i) for i in matches],
            }
            return ToolResult(content=json.dumps(payload, indent=2))

        if action == 'mark_presented':
            ids = [str(x).strip() for x in (params.get('bookmark_ids') or []) if str(x).strip()]
            one = str(params.get('bookmark_id') or '').strip()
            if one:
                ids.append(one)
            changed = _mark_presented(ctx, bookmark_ids=ids or None, new_only=bool(params.get('new_only', False)))
            return ToolResult(content=json.dumps({'count': len(changed), 'items': [_compact_view(i) for i in changed]}, indent=2))

        if action == 'enqueue_investigation':
            state_dir = _require_state_dir(ctx)
            from conversation_runtime import enqueue_agent_task

            owner_agent_id = str(params.get('owner_agent_id') or ctx.agent_id or '').strip()
            if not owner_agent_id:
                return ToolResult(content='Error: owner_agent_id is required (or current agent context must be set).', is_error=True)

            url = _normalize_url(params.get('url', ''))
            if not url:
                return ToolResult(content='Error: url is required for enqueue_investigation.', is_error=True)

            project = str(params.get('project') or '').strip() or None
            note = str(params.get('text') or '').strip()
            title = str(params.get('title') or '').strip() or f'Investigate X post: {url}'
            instruction = _default_investigation_instruction(url, note, project or '', max_items=int(params.get('max_items_per_run') or 10))

            task = enqueue_agent_task(
                state_dir,
                owner_agent_id=owner_agent_id,
                instruction=instruction,
                title=title,
                project=project,
                priority='normal',
            )
            return ToolResult(content=json.dumps({'queued_task': task}, indent=2))

        if action == 'capture_idea':
            state_dir = _require_state_dir(ctx)
            from goal_runtime import ingest_idea

            agent_id = str(params.get('owner_agent_id') or ctx.agent_id or '').strip()
            if not agent_id:
                return ToolResult(content='Error: owner_agent_id is required (or current agent context must be set).', is_error=True)

            project = str(params.get('project') or '').strip()
            text = str(params.get('text') or '').strip()
            if not project:
                return ToolResult(content='Error: project is required for capture_idea.', is_error=True)
            if not text:
                url = _normalize_url(params.get('url', ''))
                title = str(params.get('title') or '').strip()
                if title or url:
                    parts = [p for p in [title, url] if p]
                    text = ' — '.join(parts)
            if not text:
                return ToolResult(content='Error: text (or title/url) is required for capture_idea.', is_error=True)

            result = ingest_idea(state_dir, agent_id=agent_id, project=project, text=text)
            return ToolResult(content=json.dumps(result, indent=2))

        if action == 'schedule_bookmarks_review':
            state_dir = _require_state_dir(ctx)
            from conversation_runtime import enqueue_agent_task

            owner_agent_id = str(params.get('owner_agent_id') or ctx.agent_id or '').strip()
            if not owner_agent_id:
                return ToolResult(content='Error: owner_agent_id is required (or current agent context must be set).', is_error=True)

            interval = int(params.get('interval_minutes') or 60)
            if interval <= 0:
                return ToolResult(content='Error: interval_minutes must be > 0.', is_error=True)

            limit = max(1, int(params.get('max_items_per_run') or 10))
            project = str(params.get('project') or '').strip() or None
            title = str(params.get('title') or '').strip() or 'Review X bookmarks'
            instruction = (
                'Review X bookmarks using the X tool.\n'
                f'Call X with action=fetch_new_bookmarks and inspect up to {limit} new bookmarked posts this run.\n'
                'Only investigate bookmarks that do not already have stored investigation results.\n'
                'For each promising item: investigate linked resources, then either implement immediately, enqueue follow-up work, or capture a backlog idea.\n'
                'After each investigated bookmark, save the result with X action=save_investigation so the user can query it later.\n'
                'Finish with a concise summary of promising items, actions taken, and deferred ideas.'
            )
            task = enqueue_agent_task(
                state_dir,
                owner_agent_id=owner_agent_id,
                instruction=instruction,
                title=title,
                project=project,
                priority='normal',
                interval_minutes=interval,
            )
            return ToolResult(content=json.dumps({'scheduled_task': task}, indent=2))

        return ToolResult(content=f'Error: unknown action "{action}".', is_error=True)

    except Exception as e:
        return ToolResult(content=f'X tool error: {e}', is_error=True)
