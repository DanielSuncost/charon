"""Paper tool — scholarly paper search and metadata retrieval for Libris.

Backends in v1:
- arXiv API / arXiv export feed
- Semantic Scholar Graph API (best effort, no key required for light usage)
- OpenAlex API

The tool normalizes results into a common paper-shaped record so Libris can
use it for source discovery before persisting canonical source records via
the Research tool.
"""
from __future__ import annotations

import re
import textwrap
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote_plus

from tools import ToolContext, ToolResult


PAPER_TOOL_DEF = {
    'name': 'Paper',
    'description': (
        'Search scholarly papers and retrieve normalized metadata. '
        'Backends: arXiv, Semantic Scholar, OpenAlex. '
        'Use this for technical research, literature review, and academic discovery.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['search', 'lookup'],
                'description': 'search: search papers. lookup: retrieve one paper by id/url/title hint.',
            },
            'query': {
                'type': 'string',
                'description': 'Search query for paper discovery.',
            },
            'backend': {
                'type': 'string',
                'enum': ['auto', 'arxiv', 'semanticscholar', 'openalex'],
                'description': 'Preferred backend. auto tries multiple backends.',
            },
            'limit': {
                'type': 'number',
                'description': 'Maximum results to return. Default: 5.',
            },
            'paper_id': {
                'type': 'string',
                'description': 'Paper identifier or URL for lookup.',
            },
            'recency_days': {
                'type': 'number',
                'description': 'Optional recency hint for future filtering. Ignored by some backends in v1.',
            },
        },
        'required': ['action'],
    },
}


def _normalize_whitespace(text: str) -> str:
    return re.sub(r'\s+', ' ', (text or '').strip())


def _fmt_paper(p: dict[str, Any], idx: int | None = None) -> list[str]:
    prefix = f'{idx}. ' if idx is not None else ''
    lines = [f'{prefix}{p.get("title", "(untitled)")}']
    if p.get('authors'):
        lines.append(f'   Authors: {", ".join(p.get("authors")[:8])}')
    meta = []
    if p.get('published_at'):
        meta.append(str(p['published_at']))
    if p.get('venue'):
        meta.append(str(p['venue']))
    if p.get('backend'):
        meta.append(f'via {p["backend"]}')
    if meta:
        lines.append(f'   Meta: {" | ".join(meta)}')
    if p.get('url'):
        lines.append(f'   {p["url"]}')
    if p.get('abstract'):
        lines.append(f'   {_normalize_whitespace(p["abstract"])[:280]}')
    return lines


def _search_arxiv(query: str, limit: int = 5) -> list[dict[str, Any]]:
    import httpx

    url = (
        'http://export.arxiv.org/api/query?'
        f'search_query=all:{quote_plus(query)}&start=0&max_results={max(1, min(limit, 20))}&sortBy=submittedDate&sortOrder=descending'
    )
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(url, headers={'User-Agent': 'Charon-Libris/0.1'})
            if resp.status_code != 200:
                return []
    except Exception:
        return []

    try:
        root = ET.fromstring(resp.text)
    except Exception:
        return []

    ns = {'a': 'http://www.w3.org/2005/Atom'}
    results = []
    for entry in root.findall('a:entry', ns):
        title = _normalize_whitespace(entry.findtext('a:title', default='', namespaces=ns))
        summary = _normalize_whitespace(entry.findtext('a:summary', default='', namespaces=ns))
        published = _normalize_whitespace(entry.findtext('a:published', default='', namespaces=ns))
        paper_id = _normalize_whitespace(entry.findtext('a:id', default='', namespaces=ns))
        authors = [_normalize_whitespace(a.findtext('a:name', default='', namespaces=ns)) for a in entry.findall('a:author', ns)]
        authors = [a for a in authors if a]
        pdf_url = ''
        page_url = paper_id
        for link in entry.findall('a:link', ns):
            href = link.attrib.get('href', '').strip()
            title_attr = link.attrib.get('title', '').strip().lower()
            if title_attr == 'pdf' or href.endswith('.pdf'):
                pdf_url = href
        results.append({
            'backend': 'arxiv',
            'paper_id': paper_id.rsplit('/', 1)[-1] if paper_id else '',
            'title': title,
            'authors': authors,
            'abstract': summary,
            'published_at': published[:10] if published else '',
            'venue': 'arXiv',
            'url': page_url,
            'pdf_url': pdf_url,
        })
    return results[:limit]


def _search_semantic_scholar(query: str, limit: int = 5) -> list[dict[str, Any]]:
    import httpx

    fields = 'title,authors,abstract,year,venue,url,externalIds'
    url = 'https://api.semanticscholar.org/graph/v1/paper/search'
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(url, params={'query': query, 'limit': max(1, min(limit, 10)), 'fields': fields})
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception:
        return []

    out = []
    for row in (data.get('data') or [])[:limit]:
        authors = [a.get('name', '').strip() for a in (row.get('authors') or []) if a.get('name')]
        ext = row.get('externalIds') or {}
        paper_id = ext.get('ArXiv') or ext.get('DOI') or ''
        out.append({
            'backend': 'semanticscholar',
            'paper_id': paper_id,
            'title': _normalize_whitespace(row.get('title', '')),
            'authors': authors,
            'abstract': _normalize_whitespace(row.get('abstract', '')),
            'published_at': str(row.get('year') or ''),
            'venue': _normalize_whitespace(row.get('venue', '')),
            'url': row.get('url', '') or '',
            'pdf_url': '',
        })
    return out


def _search_openalex(query: str, limit: int = 5) -> list[dict[str, Any]]:
    import httpx

    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(
                'https://api.openalex.org/works',
                params={'search': query, 'per-page': max(1, min(limit, 10)), 'sort': 'publication_date:desc'},
                headers={'User-Agent': 'Charon-Libris/0.1'},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception:
        return []

    out = []
    for row in (data.get('results') or [])[:limit]:
        authors = []
        for auth in (row.get('authorships') or [])[:10]:
            author = (auth.get('author') or {}).get('display_name', '').strip()
            if author:
                authors.append(author)
        title = _normalize_whitespace(row.get('display_name', ''))
        abstract = ''
        inv = row.get('abstract_inverted_index') or {}
        if inv:
            try:
                max_pos = max((max(pos_list) for pos_list in inv.values() if pos_list), default=-1)
                words = [''] * (max_pos + 1)
                for word, pos_list in inv.items():
                    for pos in pos_list:
                        if 0 <= pos < len(words):
                            words[pos] = word
                abstract = _normalize_whitespace(' '.join(words))
            except Exception:
                abstract = ''
        out.append({
            'backend': 'openalex',
            'paper_id': str(row.get('id') or '').rsplit('/', 1)[-1],
            'title': title,
            'authors': authors,
            'abstract': abstract,
            'published_at': str(row.get('publication_date') or row.get('publication_year') or ''),
            'venue': _normalize_whitespace(((row.get('primary_location') or {}).get('source') or {}).get('display_name', '')),
            'url': row.get('doi') or (row.get('primary_location') or {}).get('landing_page_url', '') or '',
            'pdf_url': (row.get('primary_location') or {}).get('pdf_url', '') or '',
        })
    return out


def _lookup_arxiv(paper_id: str) -> list[dict[str, Any]]:
    clean = paper_id.strip()
    if not clean:
        return []
    if 'arxiv.org' in clean:
        m = re.search(r'arxiv\.org/(?:abs|pdf)/([^/?#]+)', clean)
        if m:
            clean = m.group(1).replace('.pdf', '')
    return _search_arxiv(f'id:{clean}', limit=1)


def execute_paper(params: dict, ctx: ToolContext) -> ToolResult:
    action = str(params.get('action', '')).strip().lower()
    backend = str(params.get('backend', 'auto')).strip().lower() or 'auto'
    limit = int(params.get('limit') or 5)

    if action == 'search':
        query = str(params.get('query', '')).strip()
        if not query:
            return ToolResult(content='Error: query is required for search.', is_error=True)

        results: list[dict[str, Any]] = []
        if backend in ('auto', 'arxiv'):
            results.extend(_search_arxiv(query, limit if backend == 'arxiv' else max(limit, 3)))
        if backend in ('auto', 'semanticscholar') and len(results) < limit:
            results.extend(_search_semantic_scholar(query, limit))
        if backend in ('auto', 'openalex') and len(results) < limit:
            results.extend(_search_openalex(query, limit))

        dedup = []
        seen = set()
        for row in results:
            key = (row.get('title', '').lower(), tuple(row.get('authors') or [])[:2])
            if key in seen:
                continue
            seen.add(key)
            dedup.append(row)
        dedup = dedup[:limit]

        if not dedup:
            return ToolResult(content=f'No papers found for: {query}')

        lines = [f'Paper search: "{query}" ({len(dedup)} results)\n']
        for i, row in enumerate(dedup, 1):
            lines.extend(_fmt_paper(row, i))
            lines.append('')
        return ToolResult(content='\n'.join(lines), details={'results': dedup})

    if action == 'lookup':
        paper_id = str(params.get('paper_id') or params.get('query') or '').strip()
        if not paper_id:
            return ToolResult(content='Error: paper_id is required for lookup.', is_error=True)

        results = []
        if backend in ('auto', 'arxiv'):
            results = _lookup_arxiv(paper_id)
        if not results and backend in ('auto', 'semanticscholar'):
            results = _search_semantic_scholar(paper_id, 1)
        if not results and backend in ('auto', 'openalex'):
            results = _search_openalex(paper_id, 1)

        if not results:
            return ToolResult(content=f'No paper found for: {paper_id}')

        row = results[0]
        return ToolResult(content='\n'.join(_fmt_paper(row)), details=row)

    return ToolResult(content=f'Error: unknown action "{action}". Use search or lookup.', is_error=True)
