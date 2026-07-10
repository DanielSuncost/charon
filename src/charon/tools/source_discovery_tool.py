"""SourceDiscovery tool — broad lead generation for Libris.

Focuses on discovery rather than canonical persistence.
Useful for finding promising sources before deeper reading.

v1 backends:
- Papers with Code trending / search pages via web search hints
- GitHub repository search API
- official source discovery via targeted web search
- digest/trending discovery via targeted web search
"""
from __future__ import annotations

from typing import Any

from charon.tools import ToolContext, ToolResult


SOURCE_DISCOVERY_TOOL_DEF = {
    'name': 'SourceDiscovery',
    'description': (
        'Broad source discovery for Libris. Find promising papers, repos, official sources, '
        'digests, and trend surfaces before deeper research. Use this for coordinator scouting '
        'and source lead generation.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['discover', 'repo_search', 'official_sources', 'digest_search'],
                'description': 'Discovery mode.',
            },
            'query': {
                'type': 'string',
                'description': 'Discovery query.',
            },
            'limit': {
                'type': 'number',
                'description': 'Maximum results. Default: 5.',
            },
        },
        'required': ['action', 'query'],
    },
}


def _fmt_result(r: dict[str, Any], i: int) -> list[str]:
    lines = [f'{i}. {r.get("title", "(untitled)")}']
    if r.get('url'):
        lines.append(f'   {r["url"]}')
    meta = []
    if r.get('source_type'):
        meta.append(str(r['source_type']))
    if r.get('backend'):
        meta.append(f'via {r["backend"]}')
    if r.get('score_hint'):
        meta.append(f'score={r["score_hint"]}')
    if meta:
        lines.append(f'   Meta: {" | ".join(meta)}')
    if r.get('snippet'):
        lines.append(f'   {str(r["snippet"])[:240]}')
    return lines


def _github_repo_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    import httpx

    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(
                'https://api.github.com/search/repositories',
                params={'q': query, 'sort': 'stars', 'order': 'desc', 'per_page': max(1, min(limit, 10))},
                headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'Charon-Libris/0.1'},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception:
        return []

    out = []
    for row in (data.get('items') or [])[:limit]:
        out.append({
            'title': row.get('full_name', ''),
            'url': row.get('html_url', ''),
            'snippet': row.get('description', '') or '',
            'source_type': 'repo',
            'backend': 'github',
            'score_hint': f"stars:{row.get('stargazers_count', 0)}",
        })
    return out


def _targeted_web_search(query: str, site_filters: list[str], limit: int = 5, source_type: str = 'web') -> list[dict[str, Any]]:
    try:
        from charon.tools.web_tool import _search_ddg, _search_brave, _search_searxng
        from charon.infra import config
        import os
    except Exception:
        return []

    scoped_query = query
    if site_filters:
        scoped_query += ' ' + ' '.join(f'site:{s}' for s in site_filters)

    brave_key = os.environ.get('BRAVE_SEARCH_API_KEY', '').strip()
    searxng_url = config.searxng_url()
    if brave_key:
        rows = _search_brave(scoped_query, brave_key, limit)
    elif searxng_url:
        rows = _search_searxng(scoped_query, searxng_url, limit)
    else:
        rows = _search_ddg(scoped_query, limit)

    out = []
    for row in rows[:limit]:
        out.append({
            'title': row.get('title', ''),
            'url': row.get('url', ''),
            'snippet': row.get('snippet', '') or '',
            'source_type': source_type,
            'backend': 'web-search',
            'score_hint': 'heuristic',
        })
    return out


def _official_sources(query: str, limit: int = 5) -> list[dict[str, Any]]:
    domains = [
        'openai.com', 'anthropic.com', 'deepmind.google', 'ai.meta.com', 'research.nvidia.com',
        'huggingface.co', 'pytorch.org', 'tensorflow.org', 'microsoft.com', 'apple.com',
    ]
    return _targeted_web_search(query, domains, limit=limit, source_type='official')


def _digest_sources(query: str, limit: int = 5) -> list[dict[str, Any]]:
    # Discovery surface, not canonical truth.
    domains = [
        'paperswithcode.com', 'huggingface.co', 'arxiv.org', 'github.com', 'semianalysis.com',
    ]
    boosted_query = f'{query} trending digest recent papers'
    return _targeted_web_search(boosted_query, domains, limit=limit, source_type='digest')


def _discover(query: str, limit: int = 5) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    results.extend(_digest_sources(query, limit=max(2, limit // 2)))
    if len(results) < limit:
        results.extend(_official_sources(query, limit=limit))
    if len(results) < limit:
        results.extend(_github_repo_search(query, limit=limit))

    dedup = []
    seen = set()
    for row in results:
        url = str(row.get('url') or '').strip()
        key = url or str(row.get('title') or '').lower()
        if not key or key in seen:
            continue
        seen.add(key)
        dedup.append(row)
    return dedup[:limit]



def execute_source_discovery(params: dict, ctx: ToolContext) -> ToolResult:
    action = str(params.get('action', '')).strip().lower()
    query = str(params.get('query', '')).strip()
    limit = int(params.get('limit') or 5)

    if not query:
        return ToolResult(content='Error: query is required.', is_error=True)

    if action == 'repo_search':
        results = _github_repo_search(query, limit)
    elif action == 'official_sources':
        results = _official_sources(query, limit)
    elif action == 'digest_search':
        results = _digest_sources(query, limit)
    elif action == 'discover':
        results = _discover(query, limit)
    else:
        return ToolResult(content=f'Error: unknown action "{action}".', is_error=True)

    if not results:
        return ToolResult(content=f'No discovery results found for: {query}')

    lines = [f'Source discovery: "{query}" ({len(results)} results)\n']
    for i, row in enumerate(results, 1):
        lines.extend(_fmt_result(row, i))
        lines.append('')
    return ToolResult(content='\n'.join(lines), details={'results': results})
