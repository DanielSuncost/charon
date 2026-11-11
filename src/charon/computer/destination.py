"""Where a desktop capture may go: a local model, and nowhere else.

This is a Python-side policy, not a boundary. Once this process holds a
capture, the helper that took it cannot see where it goes; it only records
the destination this module states, shows it in its approval dialog, and
refuses a capture whose stated destination differs from the one the user
approved. What makes the rule hold is that every capture is taken and
attached through the functions here, which decide the destination from the
endpoint the request will actually reach, never from a provider's name:

* local means the endpoint's host is a loopback address (or ``localhost``
  resolving only to loopback). A provider called "local" pointing at a LAN
  or cloud host is not local.
* anything that cannot be determined is not local (fail closed).
* a capture is attached to a request only if that request's endpoint is the
  same local destination the capture was taken for.

Limits, stated plainly: a loopback server that itself forwards to a cloud
API (a proxy on 127.0.0.1) looks local here; and code in this process that
bypasses these functions is not stopped by them.
"""
from __future__ import annotations

import base64
import ipaddress
import socket
import urllib.parse
from dataclasses import dataclass

LOCAL_KIND = 'local-loopback'


class CaptureRefused(RuntimeError):
    """A capture was not taken or not attached because its destination is not
    (or cannot be shown to be) a local model."""


@dataclass(frozen=True)
class Destination:
    local: bool
    label: str
    reason: str

    def terms(self) -> dict:
        """The terms stated to the helper. Only ever built for a local one."""
        if not self.local:
            raise CaptureRefused(self.reason)
        return {'destination': self.label, 'destination_kind': LOCAL_KIND}


def _host_is_loopback(host: str) -> bool:
    host = host.strip('[]').lower()
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if host != 'localhost':
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addrs = {info[4][0] for info in infos}
    return bool(addrs) and all(ipaddress.ip_address(a.split('%')[0]).is_loopback for a in addrs)


def destination_for(provider, model) -> Destination:
    """The destination of a request sent with ``provider`` and ``model``."""
    try:
        from charon.providers.provider_bridge import describe_provider_endpoint
        endpoint = describe_provider_endpoint(provider, model)
    except Exception as exc:  # noqa: BLE001 - any failure means "unknown", which is refused
        return Destination(False, '', f'cannot tell where the request goes ({type(exc).__name__}); not sending a screenshot')
    base_url = str(endpoint.get('base_url') or '').strip()
    model_id = str(endpoint.get('model_id') or '')
    label = f'{endpoint.get("provider") or "?"} {model_id} @ {base_url}'.strip()
    if not base_url:
        return Destination(False, label, 'the request has no known endpoint; not sending a screenshot')
    try:
        parts = urllib.parse.urlsplit(base_url)
        host = parts.hostname or ''
    except ValueError:
        return Destination(False, label, f'unreadable endpoint {base_url!r}; not sending a screenshot')
    if parts.scheme not in ('http', 'https') or not host:
        return Destination(False, label, f'unreadable endpoint {base_url!r}; not sending a screenshot')
    if not _host_is_loopback(host):
        return Destination(False, label, f'{base_url} is not on this Mac; screenshots go only to a local model')
    return Destination(True, label, 'loopback endpoint')


def request_capture_grant(helper, target_bundle_id: str, minutes: float, provider, model, reason: str = '') -> dict:
    """Ask the user for a capture grant tied to this model's local endpoint.
    Refused here, before the user is asked, when the endpoint is not local."""
    dest = destination_for(provider, model)
    return helper.request_grant('desktop.observe.screen', target_bundle_id, minutes, reason, terms=dest.terms())


def capture_for(helper, target_bundle_id: str, provider, model) -> dict:
    """Take a capture for a request to ``provider``/``model``. The helper is
    not even asked unless that endpoint is local."""
    dest = destination_for(provider, model)
    reply = helper.capture(target_bundle_id, terms=dest.terms())
    cap = reply.get('capture')
    if reply.get('ok') and cap:
        cap['stated_terms'] = dest.terms()
    return reply


def image_block(capture: dict, provider, model) -> dict:
    """The message block for a capture, only if the request it joins goes to
    the same local destination the capture was taken for."""
    dest = destination_for(provider, model)
    if not dest.local:
        raise CaptureRefused(dest.reason)
    if capture.get('stated_terms') != dest.terms():
        raise CaptureRefused('this screenshot was taken for a different destination; not attaching it')
    png = capture.get('png')
    if not isinstance(png, (bytes, bytearray)) or not png:
        raise CaptureRefused('no image')
    return {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                        'data': base64.b64encode(bytes(png)).decode('ascii')}}
