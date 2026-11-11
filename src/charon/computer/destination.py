"""Where a desktop capture may go: a local model, and nowhere else.

This is a Python-side policy, not a boundary. Once this process holds a
capture, the helper that took it cannot see where it goes; it only records
the destination stated here, shows it in its approval dialog, and refuses a
capture whose stated destination differs from the one the user approved.

THE SEAM. Everything that lets a capture travel is in this module, and the
rule is applied at three points, so wiring capture into an agent loop
cannot skip it by forgetting a call:

1. Taking. ``DesktopHelper.capture`` accepts only a ``Destination`` minted by
   ``destination_for`` (direct construction raises) and only a local one.
   It returns a ``SealedCapture``: no bytes attribute, no repr of the
   pixels, not picklable.
2. Attaching. ``image_block`` is the only public way to get the pixels out
   of a ``SealedCapture``, and only for a request whose endpoint is the same
   local destination the capture was taken for. It registers the block's
   data.
3. Sending. Every provider's ``stream()`` calls ``outgoing_capture_refusal``
   before building a request, which refuses any registered capture headed
   for an endpoint other than the one it was taken for. A test fails the
   build if a provider's ``stream()`` lacks that call, or if code outside
   this package uses the helper's raw ``capture`` verb or these private
   names.

How "local" is decided: from the endpoint the request will actually reach,
never from a provider's name. Local means the host is a loopback address
(or ``localhost`` resolving only to loopback). Anything that cannot be
determined is not local.

What still gets through, stated plainly:
* a loopback server that forwards to a cloud API (a proxy on 127.0.0.1);
* pixels that were transformed (decoded, re-encoded, cropped) after
  ``image_block``: the send check recognises the exact data it handed out;
* a capture handed to another process: the registry is per process;
* code that deliberately reaches into the private names below. Python has
  no private memory; the test catches it in this repo, nothing catches it
  in code outside it.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import socket
import urllib.parse
from collections import OrderedDict

LOCAL_KIND = 'local-loopback'
_MINT = object()          # only this module can mint destinations or unseal
_SENT_LIMIT = 256         # capture blocks remembered for the send check
_sent: OrderedDict[str, dict] = OrderedDict()   # sha256 of block data -> terms


class CaptureRefused(RuntimeError):
    """A capture was not taken, attached or sent because its destination is
    not (or cannot be shown to be) the local model it was taken for."""


class Destination:
    """Where a request goes. Minted only by ``destination_for``."""

    __slots__ = ('local', 'label', 'reason')

    def __init__(self, local: bool, label: str, reason: str, *, _mint=None):
        if _mint is not _MINT:
            raise TypeError('a Destination comes only from destination_for(provider, model)')
        object.__setattr__(self, 'local', bool(local))
        object.__setattr__(self, 'label', str(label))
        object.__setattr__(self, 'reason', str(reason))

    def __setattr__(self, name, value):
        raise AttributeError('Destination is immutable')

    def terms(self) -> dict:
        """The terms stated to the helper. Only ever built for a local one."""
        if not self.local:
            raise CaptureRefused(self.reason)
        return {'destination': self.label, 'destination_kind': LOCAL_KIND}

    def __repr__(self):
        return f'Destination(local={self.local}, {self.label!r})'


class SealedCapture:
    """A redacted screenshot whose pixels only ``image_block`` can release."""

    __slots__ = ('id', 'sha256', 'width', 'height', 'origin', 'pixels_per_point',
                 'redacted_fields', 'audit_seq', 'target', '_terms', '_open')

    def __init__(self, reply_capture: dict, target: str, destination: Destination, *, _mint=None):
        if _mint is not _MINT:
            raise TypeError('a SealedCapture comes only from DesktopHelper.capture')
        png = base64.b64decode(reply_capture['png_base64'])
        self.id = reply_capture.get('id', '')
        self.sha256 = reply_capture.get('sha256', '')
        self.width = reply_capture.get('width', 0)
        self.height = reply_capture.get('height', 0)
        self.origin = reply_capture.get('origin', {})
        self.pixels_per_point = reply_capture.get('pixels_per_point', 0)
        self.redacted_fields = reply_capture.get('redacted_fields', 0)
        self.audit_seq = reply_capture.get('audit_seq')
        self.target = target
        self._terms = destination.terms()

        def _open(key):
            if key is not _MINT:
                raise CaptureRefused('use destination.image_block to attach a capture')
            return png
        self._open = _open

    def __repr__(self):
        return f'SealedCapture({self.id}, {self.width}x{self.height}, for {self._terms["destination"]!r})'

    def __reduce__(self):
        raise TypeError('a SealedCapture cannot be pickled or copied out of this process')


def seal(reply_capture: dict, target: str, destination: Destination) -> SealedCapture:
    """For DesktopHelper.capture only."""
    return SealedCapture(reply_capture, target, destination, _mint=_MINT)


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
    def make(local, label, reason):
        return Destination(local, label, reason, _mint=_MINT)
    try:
        from charon.providers.provider_bridge import describe_provider_endpoint
        endpoint = describe_provider_endpoint(provider, model)
    except Exception as exc:  # noqa: BLE001 - any failure means "unknown", which is refused
        return make(False, '', f'cannot tell where the request goes ({type(exc).__name__}); not sending a screenshot')
    base_url = str(endpoint.get('base_url') or '').strip()
    model_id = str(endpoint.get('model_id') or '')
    label = f'{endpoint.get("provider") or "?"} {model_id} @ {base_url}'.strip()
    if not base_url:
        return make(False, label, 'the request has no known endpoint; not sending a screenshot')
    try:
        parts = urllib.parse.urlsplit(base_url)
        host = parts.hostname or ''
    except ValueError:
        return make(False, label, f'unreadable endpoint {base_url!r}; not sending a screenshot')
    if parts.scheme not in ('http', 'https') or not host:
        return make(False, label, f'unreadable endpoint {base_url!r}; not sending a screenshot')
    if not _host_is_loopback(host):
        return make(False, label, f'{base_url} is not on this Mac; screenshots go only to a local model')
    return make(True, label, 'loopback endpoint')


def request_capture_grant(helper, target_bundle_id: str, minutes: float, provider, model, reason: str = '') -> dict:
    """Ask the user for a capture grant tied to this model's local endpoint.
    Refused here, before the user is asked, when the endpoint is not local."""
    dest = destination_for(provider, model)
    return helper.request_grant('desktop.observe.screen', target_bundle_id, minutes, reason, destination=dest)


def capture_for(helper, target_bundle_id: str, provider, model) -> dict:
    """Take a capture for a request to ``provider``/``model``. The helper is
    not even asked unless that endpoint is local. On success
    ``reply['capture']`` is a ``SealedCapture``."""
    return helper.capture(target_bundle_id, destination_for(provider, model))


def _digest(data: str) -> str:
    return hashlib.sha256(data.encode('ascii', 'replace')).hexdigest()


def image_block(capture: SealedCapture, provider, model) -> dict:
    """The message block for a capture, only if the request it joins goes to
    the same local destination the capture was taken for."""
    if not isinstance(capture, SealedCapture):
        raise CaptureRefused('not a capture from the helper')
    dest = destination_for(provider, model)
    if not dest.local:
        raise CaptureRefused(dest.reason)
    if capture._terms != dest.terms():
        raise CaptureRefused('this screenshot was taken for a different destination; not attaching it')
    data = base64.b64encode(capture._open(_MINT)).decode('ascii')
    _sent[_digest(data)] = dict(capture._terms)
    while len(_sent) > _SENT_LIMIT:
        _sent.popitem(last=False)
    return {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': data}}


def _image_data(obj, out: list[str], depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(obj, str):
        if obj.startswith('data:image/') and ';base64,' in obj:
            out.append(obj.split(';base64,', 1)[1])
        return
    if isinstance(obj, dict):
        src = obj.get('source')
        if obj.get('type') == 'image' and isinstance(src, dict) and isinstance(src.get('data'), str):
            out.append(src['data'])
        for v in obj.values():
            if isinstance(v, (dict, list, tuple)) or (isinstance(v, str) and v.startswith('data:image/')):
                _image_data(v, out, depth + 1)
        return
    if isinstance(obj, (list, tuple)):
        for v in obj:
            _image_data(v, out, depth + 1)
        return
    content = getattr(obj, 'content', None)
    if content is not None:
        _image_data(content, out, depth + 1)


def outgoing_capture_refusal(messages, provider, model, system_prompt: str = '') -> str | None:
    """Called by every provider's ``stream()`` before it builds a request.
    Returns why the request must not be sent, or None. Cheap when no
    capture has been attached in this process."""
    if not _sent:
        return None
    found: list[str] = []
    _image_data(list(messages or []), found)
    _image_data(system_prompt, found)
    hits = [_sent[d] for d in map(_digest, found) if d in _sent]
    if not hits:
        return None
    dest = destination_for(provider, model)
    for terms in hits:
        if not dest.local or dest.terms() != terms:
            return (f'refused: this request carries a desktop screenshot taken for {terms["destination"]!r}, '
                    f'but it would go to {dest.label or "an unknown endpoint"!r}')
    return None
