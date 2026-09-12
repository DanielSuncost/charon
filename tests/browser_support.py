"""Shared helpers for the live-Chromium browser tests.

Two things every browser test needs: a deterministic local site (no network,
no fixtures on disk — pages are inline strings served by a thread) and a guard
that *skips* rather than fails when Playwright's Chromium is not installed
(CI installs the playwright package but never runs ``playwright install``).
"""
from __future__ import annotations

import http.server
import os
import socketserver
import threading
from pathlib import Path

import pytest

from charon.tools import ToolContext, browser_tool


class LocalSite:
    """Serve ``pages`` (path → HTML) on 127.0.0.1 from a daemon thread.

    ``url(path)`` is the normal origin; ``alt_url(path)`` serves the same
    pages from ``localhost`` (a different origin for the browser) so a page
    can embed a cross-origin iframe without leaving the machine.
    """

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self._server: socketserver.TCPServer | None = None
        self.port = 0

    def start(self) -> 'LocalSite':
        pages = self.pages

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib naming
                path = self.path.split('?')[0]
                body = pages.get(path)
                if body is not None:
                    body = body.replace('{port}', str(self.server.server_address[1]))
                self.send_response(200 if body is not None else 404)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write((body or '<p>404</p>').encode('utf-8'))

            def log_message(self, *args):  # keep pytest output clean
                return None

        socketserver.TCPServer.allow_reuse_address = True
        self._server = socketserver.TCPServer(('127.0.0.1', 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def url(self, path: str) -> str:
        return f'http://127.0.0.1:{self.port}{path}'

    def alt_url(self, path: str) -> str:
        return f'http://localhost:{self.port}{path}'


def ensure_browser_or_skip(tmp_dir: Path) -> ToolContext:
    """Launch (or reuse) the tool's Chromium; skip the test module if it cannot start."""
    os.environ['CHARON_BROWSER_HEADLESS'] = '1'
    ctx = ToolContext(project_root=tmp_dir, state_dir=tmp_dir / 'state')
    browser_tool._session_id_ctx = ''
    browser_tool._state_dir_ctx = ctx.state_dir
    try:
        browser_tool._run(browser_tool._ensure_page(), timeout=90)
    except Exception as exc:  # missing browser binary, sandbox refusal, …
        pytest.skip(f'Playwright Chromium not available: {exc}')
    return ctx


def shutdown_browser() -> None:
    try:
        browser_tool._run(browser_tool._shutdown(), timeout=30)
    except Exception:
        pass


def act(ctx: ToolContext, **params):
    return browser_tool.execute_browser(params, ctx)


def js(expression: str, frame=None):
    """Evaluate JS in the current page (or a given frame) from the test thread."""
    target = frame if frame is not None else browser_tool._page
    return browser_tool._run(target.evaluate(expression))


def frames():
    return list(browser_tool._page.frames)


def reset_tool_state() -> None:
    """Return per-test knobs to their defaults without touching the browser."""
    browser_tool._vision_backend = None
    browser_tool._set_dialog_policy('dismiss', '')
    browser_tool._dialog_log.clear()
    browser_tool._dialogs_unreported.clear()
    os.environ.pop('CHARON_BROWSER_VISION', None)
