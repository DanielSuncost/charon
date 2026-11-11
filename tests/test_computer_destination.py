"""The local-only rule for desktop captures (Python side), and the checks
that keep it from being bypassed."""
import ast
import asyncio
import re
from pathlib import Path

import pytest

from charon.computer import destination as d
from charon.computer.helper import DesktopHelper
from charon.providers import Message, ModelInfo
from charon.providers.httpx_openai import HttpxOpenAIProvider

REPO = Path(__file__).resolve().parents[1]


def _openai(url, provider='local'):
    return HttpxOpenAIProvider(base_url=url, api_key='x'), ModelInfo(provider=provider, model_id='m')


LOCAL = _openai('http://127.0.0.1:1234/v1')
CLOUD = _openai('https://api.openai.com/v1', 'openai')


def _sealed(dest, png=b'\x89PNG fake'):
    import base64
    return d.seal({'id': 'c-1', 'png_base64': base64.b64encode(png).decode(), 'width': 1, 'height': 1},
                  'com.apple.Notes', dest)


@pytest.mark.parametrize('url', ['http://127.0.0.1:1234/v1', 'http://[::1]:8080/v1', 'http://127.8.0.1/v1'])
def test_loopback_endpoints_are_local(url):
    assert d.destination_for(*_openai(url)).local


@pytest.mark.parametrize('url,provider', [
    ('http://10.0.0.5:1234/v1', 'local'),          # named local, but on the LAN
    ('http://192.168.1.2:11434/v1', 'ollama'),
    ('https://api.openai.com/v1', 'openai'),
    ('https://openrouter.ai/api/v1', 'local'),
    ('http://127.0.0.1.evil.example/v1', 'local'),
])
def test_anything_off_this_mac_is_not_local(url, provider):
    dest = d.destination_for(*_openai(url, provider))
    assert not dest.local and dest.reason
    with pytest.raises(d.CaptureRefused):
        dest.terms()


def test_unknown_endpoint_fails_closed():
    assert not d.destination_for(object(), None).local


def test_destinations_and_captures_cannot_be_forged():
    with pytest.raises(TypeError):
        d.Destination(True, 'mine', 'trust me')
    with pytest.raises(TypeError):
        d.SealedCapture({'png_base64': ''}, 't', d.destination_for(*LOCAL))
    dest = d.destination_for(*LOCAL)
    with pytest.raises(AttributeError):
        dest.local = False
    cap = _sealed(dest)
    assert not hasattr(cap, 'png') and 'PNG' not in repr(cap)
    with pytest.raises(d.CaptureRefused):
        cap._open(object())
    import pickle
    with pytest.raises(TypeError):
        pickle.dumps(cap)


def test_cloud_request_never_reaches_the_helper():
    class NoHelper(DesktopHelper):
        def call(self, *a, **k):
            raise AssertionError('the helper must not be asked')
    h = NoHelper()
    with pytest.raises(d.CaptureRefused):
        d.capture_for(h, 'com.apple.Notes', *CLOUD)
    with pytest.raises(d.CaptureRefused):
        d.request_capture_grant(h, 'com.apple.Notes', 5, *CLOUD)
    with pytest.raises(d.CaptureRefused):
        h.capture('com.apple.Notes', {'destination': 'x', 'destination_kind': 'local-loopback'})
    with pytest.raises(d.CaptureRefused):
        h.request_grant('desktop.observe.screen', 'com.apple.Notes', 5)


def test_capture_attaches_only_to_the_destination_it_was_taken_for():
    cap = _sealed(d.destination_for(*LOCAL))
    assert d.image_block(cap, *LOCAL)['type'] == 'image'
    for other in (CLOUD, _openai('http://127.0.0.1:9999/v1')):
        with pytest.raises(d.CaptureRefused):
            d.image_block(cap, *other)
    with pytest.raises(d.CaptureRefused):
        d.image_block({'png': b'x'}, *LOCAL)


def _first_delta(provider, model, messages, system=''):
    async def run():
        async for delta in provider.stream(messages, model, system):
            return delta
    return asyncio.run(run())


def test_a_provider_refuses_to_send_a_capture_to_another_endpoint():
    block = d.image_block(_sealed(d.destination_for(*LOCAL), png=b'\x89PNG unique-1'), *LOCAL)
    msgs = [Message(role='user', content=[{'type': 'text', 'text': 'look'}, block])]
    delta = _first_delta(*CLOUD, msgs)
    assert delta.type == 'error' and 'desktop screenshot' in delta.error
    other_local = _openai('http://127.0.0.1:9999/v1')
    assert 'desktop screenshot' in _first_delta(*other_local, msgs).error
    # Smuggled as a data URL in the system prompt: still recognised.
    url = 'data:image/png;base64,' + block['source']['data']
    assert 'desktop screenshot' in _first_delta(*CLOUD, [Message(role='user', content='hi')], url).error
    assert d.outgoing_capture_refusal(msgs, *LOCAL) is None


# ── Structural: the rule cannot be skipped by forgetting a call

def _python_files():
    for root in (REPO / 'src', REPO / 'apps'):
        yield from (p for p in root.rglob('*.py') if '.venv' not in p.parts and 'node_modules' not in p.parts)


def test_every_provider_stream_checks_outgoing_captures():
    missing = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            if any(getattr(b, 'id', getattr(b, 'attr', '')) == 'Protocol' for b in cls.bases):
                continue   # the interface itself has no body
            for fn in cls.body:
                if not (isinstance(fn, ast.AsyncFunctionDef) and fn.name == 'stream'):
                    continue
                args = [a.arg for a in fn.args.args]
                if args[:2] != ['self', 'messages']:
                    continue
                calls = {getattr(c.func, 'id', getattr(c.func, 'attr', None))
                         for c in ast.walk(fn) if isinstance(c, ast.Call)}
                if 'outgoing_capture_refusal' not in calls:
                    missing.append(f'{path.relative_to(REPO)}:{cls.name}.stream')
    assert not missing, f'provider stream() without the capture send check: {missing}'


FORBIDDEN = re.compile(r"""_MINT|\._open\(|\._terms\b|png_base64|\bseal\(|\.call\(\s*['"]capture['"]""")
ALLOWED = {Path('src/charon/computer/destination.py'), Path('src/charon/computer/helper.py')}


def test_nothing_outside_the_seam_touches_raw_captures():
    offenders = []
    for path in _python_files():
        rel = path.relative_to(REPO)
        if rel in ALLOWED:
            continue
        for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if FORBIDDEN.search(line):
                offenders.append(f'{rel}:{n}: {line.strip()}')
    assert not offenders, 'captures must go through charon.computer.destination:\n' + '\n'.join(offenders)
