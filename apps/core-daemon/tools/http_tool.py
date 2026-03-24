"""HTTP tool — make HTTP requests for API testing and data fetching.

Uses httpx (already a dependency for providers). No additional deps needed.
"""
from __future__ import annotations

import json
from typing import Any

from tools import ToolContext, ToolResult

HTTP_TOOL_DEF = {
    'name': 'Http',
    'description': (
        'Make HTTP requests. Test APIs, check endpoints, fetch data. '
        'Returns status code, headers, and response body. '
        'Body is truncated to 50KB for large responses.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'method': {
                'type': 'string',
                'enum': ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD'],
                'description': 'HTTP method.',
            },
            'url': {
                'type': 'string',
                'description': 'Full URL to request.',
            },
            'headers': {
                'type': 'object',
                'description': 'Request headers as key-value pairs.',
            },
            'body': {
                'type': 'string',
                'description': 'Request body (for POST/PUT/PATCH).',
            },
            'timeout': {
                'type': 'number',
                'description': 'Timeout in seconds (default: 30).',
            },
        },
        'required': ['url'],
    },
}

MAX_BODY = 50_000


def execute_http(params: dict, ctx: ToolContext) -> ToolResult:
    """Execute an HTTP request."""
    import httpx

    url = str(params.get('url', '')).strip()
    if not url:
        return ToolResult(content='Error: url is required.', is_error=True)

    method = str(params.get('method', 'GET')).upper()
    headers = params.get('headers') or {}
    body = params.get('body')
    timeout = float(params.get('timeout') or 30)

    if not isinstance(headers, dict):
        headers = {}

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            kwargs: dict[str, Any] = {'headers': headers}
            if body and method in ('POST', 'PUT', 'PATCH'):
                # Try to detect JSON
                try:
                    json.loads(body)
                    kwargs['content'] = body
                    if 'content-type' not in {k.lower() for k in headers}:
                        kwargs['headers'] = {**headers, 'Content-Type': 'application/json'}
                except (json.JSONDecodeError, TypeError):
                    kwargs['content'] = body

            resp = client.request(method, url, **kwargs)

    except httpx.TimeoutException:
        return ToolResult(content=f'Error: Request timed out after {timeout}s', is_error=True)
    except httpx.ConnectError as e:
        return ToolResult(content=f'Error: Connection failed: {e}', is_error=True)
    except Exception as e:
        return ToolResult(content=f'Error: {e}', is_error=True)

    # Format response
    truncated = False
    resp_body = resp.text
    if len(resp_body) > MAX_BODY:
        resp_body = resp_body[:MAX_BODY]
        truncated = True

    # Build output
    lines = [
        f'HTTP {resp.status_code} {resp.reason_phrase}',
        f'URL: {resp.url}',
    ]

    # Show relevant headers
    for key in ('content-type', 'content-length', 'location', 'set-cookie',
                'x-ratelimit-remaining', 'retry-after'):
        val = resp.headers.get(key)
        if val:
            lines.append(f'{key}: {val}')

    lines.append('')
    lines.append(resp_body)

    if truncated:
        lines.append(f'\n[Response truncated to {MAX_BODY // 1000}KB]')

    content = '\n'.join(lines)
    is_error = resp.status_code >= 400

    return ToolResult(content=content, is_error=is_error, truncated=truncated)
