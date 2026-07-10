"""Tests for the Web search and extraction tool."""

from charon.tools import ToolContext
from charon.tools.web_tool import execute_web, html_to_text, _search_ddg


# ── HTML to text ────────────────────────────────────────────────────

def test_html_to_text_basic():
    html = '<html><body><h1>Hello</h1><p>World</p></body></html>'
    text = html_to_text(html)
    assert 'Hello' in text
    assert 'World' in text


def test_html_to_text_strips_scripts():
    html = '<div>Before<script>alert("xss")</script>After</div>'
    text = html_to_text(html)
    assert 'Before' in text
    assert 'After' in text
    assert 'alert' not in text


def test_html_to_text_strips_style():
    html = '<div>Text<style>.hidden{display:none}</style>More</div>'
    text = html_to_text(html)
    assert 'Text' in text
    assert 'display' not in text


def test_html_to_text_preserves_links():
    html = '<p>Visit <a href="https://example.com">Example</a> now</p>'
    text = html_to_text(html)
    assert 'Example' in text
    assert 'https://example.com' in text


def test_html_to_text_headings():
    html = '<h1>Title</h1><h2>Subtitle</h2><p>Content</p>'
    text = html_to_text(html)
    assert '# Title' in text
    assert '## Subtitle' in text


def test_html_to_text_lists():
    html = '<ul><li>One</li><li>Two</li><li>Three</li></ul>'
    text = html_to_text(html)
    assert '- One' in text
    assert '- Two' in text


def test_html_to_text_empty():
    assert html_to_text('') == ''
    assert html_to_text('<html></html>') == ''


# ── Web search (live, may be flaky) ─────────────────────────────────

def test_ddg_search_returns_results():
    """Live test — may fail if DDG blocks or network is down."""
    results = _search_ddg('python programming language', max_results=3)
    # Don't assert specific results — just check structure
    if results:  # may be empty if DDG blocks
        assert isinstance(results[0], dict)
        assert 'title' in results[0]
        assert 'url' in results[0]


# ── Tool execution ──────────────────────────────────────────────────

def test_web_search_action(tmp_path):
    ctx = ToolContext(project_root=tmp_path)
    result = execute_web({'action': 'search', 'query': 'python httpx library'}, ctx)
    # May succeed or fail depending on network
    assert hasattr(result, 'content')
    assert not result.is_error or 'No results' in result.content


def test_web_extract_action(tmp_path):
    """Test extracting from a known URL."""
    ctx = ToolContext(project_root=tmp_path)
    result = execute_web({'action': 'extract', 'url': 'http://127.0.0.1:1234/v1/models'}, ctx)
    # LM Studio may or may not be running
    assert hasattr(result, 'content')


def test_web_missing_query(tmp_path):
    ctx = ToolContext(project_root=tmp_path)
    result = execute_web({'action': 'search'}, ctx)
    assert result.is_error
    assert 'query is required' in result.content


def test_web_missing_url(tmp_path):
    ctx = ToolContext(project_root=tmp_path)
    result = execute_web({'action': 'extract'}, ctx)
    assert result.is_error
    assert 'url is required' in result.content


def test_web_unknown_action(tmp_path):
    ctx = ToolContext(project_root=tmp_path)
    result = execute_web({'action': 'crawl'}, ctx)
    assert result.is_error
