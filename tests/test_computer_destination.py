"""The local-only rule for desktop captures (Python side)."""
import pytest

from charon.computer import destination as d
from charon.providers import ModelInfo
from charon.providers.httpx_openai import HttpxOpenAIProvider


def _openai(url, provider='local'):
    return HttpxOpenAIProvider(base_url=url, api_key='x'), ModelInfo(provider=provider, model_id='m')


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
    class Weird:
        pass
    assert not d.destination_for(Weird(), ModelInfo(provider='mystery', model_id='m')).local
    assert not d.destination_for(object(), None).local


def test_cloud_request_never_reaches_the_helper():
    class NoHelper:
        def capture(self, *a, **k):
            raise AssertionError('the helper must not be asked')
        request_grant = capture
    cloud = _openai('https://api.openai.com/v1', 'openai')
    with pytest.raises(d.CaptureRefused):
        d.capture_for(NoHelper(), 'com.apple.Notes', *cloud)
    with pytest.raises(d.CaptureRefused):
        d.request_capture_grant(NoHelper(), 'com.apple.Notes', 5, *cloud)


def test_capture_attaches_only_to_the_destination_it_was_taken_for():
    local = _openai('http://127.0.0.1:1234/v1')
    terms = d.destination_for(*local).terms()
    cap = {'png': b'\x89PNG...', 'stated_terms': terms}
    assert d.image_block(cap, *local)['type'] == 'image'
    with pytest.raises(d.CaptureRefused):
        d.image_block(cap, *_openai('https://api.openai.com/v1', 'openai'))
    with pytest.raises(d.CaptureRefused):
        d.image_block(cap, *_openai('http://127.0.0.1:9999/v1'))   # a different local server
    with pytest.raises(d.CaptureRefused):
        d.image_block({'png': b'x'}, *local)                       # taken without stated terms
