"""Vision fallback: when the DOM walk exposes nothing usable, a screenshot is
described into a fixed shape and the result is clickable by coordinate.

Live-browser tests use a stubbed backend (no model calls). The backend's own
provider handling is tested in-process with fake providers.
"""
from __future__ import annotations

import asyncio
import base64
import os
from types import SimpleNamespace

import pytest

from charon.providers import ModelInfo
from charon.tools import browser_tool
from charon.tools.browser_tool import VISION_SCHEMA, VisionUnavailable, _provider_vision_backend
from browser_support import LocalSite, act, ensure_browser_or_skip, js, reset_tool_state, shutdown_browser

CANVAS_PAGE = """<!doctype html><html><head><title>paint</title></head>
<body style="margin:0"><canvas id="c" width="1280" height="800"></canvas>
<script>document.getElementById('c').onclick = e => { window.__canvas_click = [e.clientX, e.clientY]; };</script>
</body></html>"""

FORM_PAGE = """<!doctype html><html><body><input placeholder="email"><button>Send</button></body></html>"""

PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


@pytest.fixture(scope='module')
def site():
    s = LocalSite({'/canvas': CANVAS_PAGE, '/form': FORM_PAGE}).start()
    yield s
    s.stop()


@pytest.fixture(scope='module')
def browser(tmp_path_factory):
    ctx = ensure_browser_or_skip(tmp_path_factory.mktemp('browser-vision'))
    yield ctx
    shutdown_browser()


@pytest.fixture(autouse=True)
def _reset():
    reset_tool_state()
    yield
    reset_tool_state()


def _stub(answer: str = '', elements=None, calls: list | None = None):
    async def backend(png, question, schema, state_dir):
        if calls is not None:
            calls.append({'png': png, 'question': question, 'schema': schema, 'state_dir': state_dir})
        return {'summary': 'A drawing canvas.', 'answer': answer,
                'elements': elements if elements is not None else [{'label': 'Brush', 'kind': 'canvas-control', 'x': 100, 'y': 200}]}
    return backend


def test_browser_vision_fallback_triggers_when_dom_exposes_nothing(site, browser):
    calls: list = []
    browser_tool._vision_backend = _stub(calls=calls)

    state = act(browser, action='navigate', url=site.url('/canvas')).content
    assert '--- Vision fallback' in state
    assert '[v1] canvas-control "Brush" at (100, 200)' in state
    assert len(calls) == 1
    assert calls[0]['png'].startswith(PNG_MAGIC)
    assert calls[0]['schema'] is VISION_SCHEMA
    assert calls[0]['state_dir'] == browser.state_dir

    result = act(browser, action='click', ref='v1')
    assert not result.is_error and result.content.startswith('Clicked [v1]')
    assert js('() => window.__canvas_click') == [100, 200]


def test_browser_vision_fallback_not_used_when_dom_has_elements(site, browser):
    async def must_not_run(*a):
        raise AssertionError('vision backend must not run when the DOM has elements')
    browser_tool._vision_backend = must_not_run
    state = act(browser, action='navigate', url=site.url('/form')).content
    assert 'Vision fallback' not in state and 'button "Send"' in state


def test_browser_vision_action_answers_question_with_sync_stub(site, browser):
    seen = {}

    def sync_backend(png, question, schema, state_dir):      # sync callables are fine too
        seen['question'] = question
        return {'summary': 'Three bars.', 'answer': 'three', 'elements': []}

    browser_tool._vision_backend = sync_backend
    act(browser, action='navigate', url=site.url('/form'))
    result = act(browser, action='vision', text='How many bars?')
    assert not result.is_error
    assert 'Answer: three' in result.content and seen['question'] == 'How many bars?'
    assert '(no clickable elements identified' in result.content


def test_browser_vision_fallback_unavailable_is_reported_not_raised(site, browser):
    async def unavailable(*a):
        raise VisionUnavailable('no provider configured')
    browser_tool._vision_backend = unavailable
    result = act(browser, action='navigate', url=site.url('/canvas'))
    assert not result.is_error
    assert '(vision fallback unavailable: no provider configured)' in result.content


def test_browser_vision_fallback_disabled_by_env(site, browser):
    calls: list = []
    browser_tool._vision_backend = _stub(calls=calls)
    os.environ['CHARON_BROWSER_VISION'] = '0'
    state = act(browser, action='navigate', url=site.url('/canvas')).content
    assert 'Vision fallback' not in state and calls == []


def test_browser_click_at_coordinates(site, browser):
    act(browser, action='navigate', url=site.url('/canvas'))
    result = act(browser, action='click_at', x=33, y=44)
    assert not result.is_error and js('() => window.__canvas_click') == [33, 44]


# ── provider backend, no browser needed ──────────────────────────────────────

def _model():
    return ModelInfo(provider='x', model_id='m')


def test_browser_multimodal_backend_refuses_provider_that_cannot_carry_images(monkeypatch):
    class FakeOpenAI:
        pass
    FakeOpenAI.__module__ = 'charon.providers.httpx_openai'
    monkeypatch.setattr('charon.providers.provider_bridge.create_provider_and_model',
                        lambda state_dir, **kw: (FakeOpenAI(), _model(), True))
    with pytest.raises(VisionUnavailable, match='cannot carry images'):
        asyncio.run(_provider_vision_backend(b'png', 'q', VISION_SCHEMA, None))


def test_browser_multimodal_backend_reports_missing_provider(monkeypatch):
    monkeypatch.setattr('charon.providers.provider_bridge.create_provider_and_model',
                        lambda state_dir, **kw: (object(), _model(), False))
    with pytest.raises(VisionUnavailable, match='no provider configured'):
        asyncio.run(_provider_vision_backend(b'png', 'q', VISION_SCHEMA, None))


class _FakeAnthropic:
    def __init__(self, reply: str):
        self.reply = reply
        self.seen: dict = {}

    async def stream(self, messages, model, system_prompt, tools=None, thinking_level='off', max_tokens=0):
        self.seen = {'messages': messages, 'model': model, 'system': system_prompt}
        yield SimpleNamespace(type='text', text=self.reply)
        yield SimpleNamespace(type='done')


_FakeAnthropic.__module__ = 'charon.providers.httpx_anthropic'


def test_browser_multimodal_backend_sends_image_block_and_validates_schema(monkeypatch):
    fake = _FakeAnthropic('```json\n{"summary": "A chart", "elements": [{"label": "Export", "kind": "button", "x": 10, "y": 20}]}\n```')
    monkeypatch.setattr('charon.providers.provider_bridge.create_provider_and_model',
                        lambda state_dir, **kw: (fake, _model(), True))
    png = PNG_MAGIC + b'fake'
    result = asyncio.run(_provider_vision_backend(png, 'Where is export?', VISION_SCHEMA, None))
    assert result['summary'] == 'A chart' and result['elements'][0]['label'] == 'Export'

    content = fake.seen['messages'][0].content
    assert isinstance(content, list) and content[0]['type'] == 'image'
    assert base64.b64decode(content[0]['source']['data']) == png
    assert 'Where is export?' in content[1]['text']
    assert fake.seen['model'].supports_images is True


def test_browser_multimodal_backend_rejects_answer_that_violates_schema(monkeypatch):
    fake = _FakeAnthropic('{"summary": "no elements key"}')
    monkeypatch.setattr('charon.providers.provider_bridge.create_provider_and_model',
                        lambda state_dir, **kw: (fake, _model(), True))
    with pytest.raises(VisionUnavailable, match='did not match schema'):
        asyncio.run(_provider_vision_backend(b'png', '', VISION_SCHEMA, None))


def test_browser_multimodal_backend_rejects_non_json(monkeypatch):
    fake = _FakeAnthropic('I see a chart with an export button.')
    monkeypatch.setattr('charon.providers.provider_bridge.create_provider_and_model',
                        lambda state_dir, **kw: (fake, _model(), True))
    with pytest.raises(VisionUnavailable, match='no JSON object'):
        asyncio.run(_provider_vision_backend(b'png', '', VISION_SCHEMA, None))
