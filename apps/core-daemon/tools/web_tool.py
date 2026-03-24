"""Web search and page extraction tool.

Two actions:
  search — search the web via DuckDuckGo (no API key) or SearXNG
  extract — fetch a URL and extract readable text from HTML

Zero external dependencies beyond httpx (already installed for providers).
HTML-to-text uses stdlib html.parser — no beautifulsoup/html2text needed.
"""
from __future__ import annotations

import html
import json
import os
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

from tools import ToolContext, ToolResult


# ── HTML to text (stdlib only) ──────────────────────────────────────

_BLOCK_TAGS = {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
               'blockquote', 'pre', 'article', 'section', 'header', 'footer', 'dt', 'dd'}
_SKIP_TAGS = {'script', 'style', 'noscript', 'svg', 'nav', 'footer', 'iframe', 'head'}
_HEADING_TAGS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}


class _TextExtractor(HTMLParser):
    """Extract readable text from HTML, stripping tags and scripts."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0
        self.link_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self.parts.append('\n')
        if tag in _HEADING_TAGS:
            level = int(tag[1])
            self.parts.append('\n' + '#' * level + ' ')
        if tag == 'a':
            href = dict(attrs).get('href', '')
            if href and not href.startswith(('#', 'javascript:')):
                self.link_href = href
        if tag == 'li':
            self.parts.append('\n- ')

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self.parts.append('\n')
        if tag == 'a' and self.link_href:
            self.parts.append(f' ({self.link_href})')
            self.link_href = None

    def handle_data(self, data: str):
        if self.skip_depth:
            return
        self.parts.append(data)

    def get_text(self) -> str:
        raw = ''.join(self.parts)
        # Clean up whitespace
        lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
        # Collapse multiple blank lines
        result = []
        prev_blank = False
        for line in lines:
            if not line:
                if not prev_blank:
                    result.append('')
                prev_blank = True
            else:
                result.append(line)
                prev_blank = False
        return '\n'.join(result)


def html_to_text(html_content: str) -> str:
    """Convert HTML to readable text. Zero dependencies."""
    parser = _TextExtractor()
    try:
        parser.feed(html_content)
    except Exception:
        pass
    return parser.get_text()


# ── DuckDuckGo search (no API key) ─────────────────────────────────

def _search_ddg(query: str, max_results: int = 8) -> list[dict]:
    """Search DuckDuckGo HTML and extract results."""
    import httpx

    url = f'https://html.duckduckgo.com/html/?q={quote_plus(query)}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    }

    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code != 200:
                return []
    except Exception:
        return []

    # Parse results from DDG HTML
    results = []
    # DDG wraps results in <a class="result__a"> with <a class="result__snippet">
    # Simple regex extraction since we're avoiding BS4
    result_blocks = re.findall(
        r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
        r'class="result__snippet"[^>]*>(.*?)</(?:a|td|div)',
        resp.text, re.DOTALL,
    )

    for href, title_html, snippet_html in result_blocks[:max_results]:
        # Clean DDG redirect URL
        if '/l/?uddg=' in href:
            match = re.search(r'uddg=([^&]+)', href)
            if match:
                from urllib.parse import unquote
                href = unquote(match.group(1))

        title = re.sub(r'<[^>]+>', '', title_html).strip()
        snippet = re.sub(r'<[^>]+>', '', snippet_html).strip()
        snippet = html.unescape(snippet)
        title = html.unescape(title)

        if title and href:
            results.append({
                'title': title,
                'url': href,
                'snippet': snippet,
            })

    return results


def _search_brave(query: str, api_key: str, max_results: int = 8) -> list[dict]:
    """Search via Brave Search API (best quality, needs API key)."""
    import httpx

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                'https://api.search.brave.com/res/v1/web/search',
                params={'q': query, 'count': max_results},
                headers={
                    'Accept': 'application/json',
                    'Accept-Encoding': 'gzip',
                    'X-Subscription-Token': api_key,
                },
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception:
        return []

    results = []
    for r in (data.get('web', {}).get('results') or [])[:max_results]:
        results.append({
            'title': r.get('title', ''),
            'url': r.get('url', ''),
            'snippet': r.get('description', ''),
        })
    return results


def _search_searxng(query: str, base_url: str, max_results: int = 8) -> list[dict]:
    """Search via a SearXNG instance (JSON API)."""
    import httpx

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                f'{base_url.rstrip("/")}/search',
                params={'q': query, 'format': 'json', 'categories': 'general'},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception:
        return []

    results = []
    for r in (data.get('results') or [])[:max_results]:
        results.append({
            'title': r.get('title', ''),
            'url': r.get('url', ''),
            'snippet': r.get('content', ''),
        })
    return results


# ── Tool definition ─────────────────────────────────────────────────

WEB_TOOL_DEF = {
    'name': 'Web',
    'description': (
        'Search the web or extract content from a URL. '
        'Actions: search (find information), extract (fetch and read a page). '
        'Search uses DuckDuckGo by default (no API key needed). '
        'Extract fetches a URL and converts HTML to readable text.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['search', 'extract'],
                'description': 'search: web search. extract: fetch and read a URL.',
            },
            'query': {
                'type': 'string',
                'description': 'Search query (for search action).',
            },
            'url': {
                'type': 'string',
                'description': 'URL to fetch (for extract action).',
            },
            'max_results': {
                'type': 'number',
                'description': 'Max search results (default: 5).',
            },
        },
        'required': ['action'],
    },
}

MAX_EXTRACT_CHARS = 30_000


def _extract_pdf_from_response(resp, url: str) -> ToolResult:
    """Extract text from a PDF response using pdftotext."""
    import shutil
    import subprocess
    import tempfile

    pdftotext = shutil.which('pdftotext')
    if not pdftotext:
        return ToolResult(
            content='Error: pdftotext not installed. Install: sudo apt install poppler-utils',
            is_error=True,
        )

    try:
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=True) as tmp:
            tmp.write(resp.content)
            tmp.flush()
            proc = subprocess.run(
                [pdftotext, '-layout', tmp.name, '-'],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                return ToolResult(content=f'Error: pdftotext failed: {proc.stderr}', is_error=True)
            text = proc.stdout
    except subprocess.TimeoutExpired:
        return ToolResult(content='Error: PDF extraction timed out', is_error=True)
    except Exception as e:
        return ToolResult(content=f'Error extracting PDF: {e}', is_error=True)

    truncated = len(text) > MAX_EXTRACT_CHARS
    if truncated:
        text = text[:MAX_EXTRACT_CHARS] + '\n\n[Truncated]'

    return ToolResult(
        content=f'URL: {url}\nType: PDF ({len(resp.content)} bytes)\n\n{text}',
        truncated=truncated,
    )


def execute_web(params: dict, ctx: ToolContext) -> ToolResult:
    """Execute web search or page extraction."""
    action = str(params.get('action', '')).strip().lower()

    if action == 'search':
        query = str(params.get('query', '')).strip()
        if not query:
            return ToolResult(content='Error: query is required for search.', is_error=True)

        max_results = int(params.get('max_results') or 5)

        # Provider priority: Brave (best) > SearXNG (self-hosted) > DDG (zero config)
        brave_key = os.environ.get('BRAVE_SEARCH_API_KEY', '').strip()
        searxng_url = os.environ.get('CHARON_SEARXNG_URL', '').strip()

        if brave_key:
            results = _search_brave(query, brave_key, max_results)
        elif searxng_url:
            results = _search_searxng(query, searxng_url, max_results)
        else:
            results = _search_ddg(query, max_results)

        if not results:
            return ToolResult(content=f'No results found for: {query}')

        lines = [f'Web search: "{query}" ({len(results)} results)\n']
        for i, r in enumerate(results, 1):
            lines.append(f'{i}. {r["title"]}')
            lines.append(f'   {r["url"]}')
            if r.get('snippet'):
                lines.append(f'   {r["snippet"]}')
            lines.append('')

        return ToolResult(content='\n'.join(lines))

    if action == 'extract':
        url = str(params.get('url', '')).strip()
        if not url:
            return ToolResult(content='Error: url is required for extract.', is_error=True)

        import httpx

        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }

        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
        except httpx.TimeoutException:
            return ToolResult(content=f'Error: Request timed out fetching {url}', is_error=True)
        except httpx.ConnectError as e:
            return ToolResult(content=f'Error: Connection failed: {e}', is_error=True)
        except Exception as e:
            return ToolResult(content=f'Error: {e}', is_error=True)

        content_type = resp.headers.get('content-type', '')

        # PDF: download to temp file, extract with pdftotext
        if 'pdf' in content_type or url.lower().endswith('.pdf'):
            return _extract_pdf_from_response(resp, url)

        # If it's JSON, return as-is
        if 'json' in content_type:
            try:
                body = json.dumps(resp.json(), indent=2)
            except Exception:
                body = resp.text
            if len(body) > MAX_EXTRACT_CHARS:
                body = body[:MAX_EXTRACT_CHARS] + '\n\n[Truncated]'
            return ToolResult(content=f'URL: {resp.url}\nType: {content_type}\n\n{body}')

        # If it's plain text, return as-is
        if 'text/plain' in content_type:
            body = resp.text[:MAX_EXTRACT_CHARS]
            truncated = len(resp.text) > MAX_EXTRACT_CHARS
            return ToolResult(
                content=f'URL: {resp.url}\n\n{body}',
                truncated=truncated,
            )

        # HTML: extract text
        text = html_to_text(resp.text)
        truncated = len(text) > MAX_EXTRACT_CHARS
        if truncated:
            text = text[:MAX_EXTRACT_CHARS] + '\n\n[Truncated]'

        # Get page title
        title_match = re.search(r'<title[^>]*>(.*?)</title>', resp.text, re.DOTALL | re.IGNORECASE)
        title = html.unescape(title_match.group(1).strip()) if title_match else ''

        header = f'URL: {resp.url}'
        if title:
            header += f'\nTitle: {title}'

        return ToolResult(content=f'{header}\n\n{text}', truncated=truncated)

    return ToolResult(
        content=f'Error: Unknown action "{action}". Use: search, extract.',
        is_error=True,
    )
