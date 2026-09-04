"""Structured HTTP/provider error helpers shared by lightweight providers."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Mapping

import httpx

from charon.providers import StreamDelta


RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def parse_retry_after(headers: Mapping[str, str] | None) -> float | None:
    """Parse Retry-After as seconds or an HTTP date, capped by the engine."""
    if not headers:
        return None
    value = headers.get('retry-after') or headers.get('Retry-After')
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def http_error(
    message: str,
    *,
    status_code: int,
    headers: Mapping[str, str] | None = None,
    prefix: str = 'HTTP',
) -> StreamDelta:
    return StreamDelta(
        type='error',
        error=f'{prefix} {status_code}: {message}',
        status_code=status_code,
        error_code='http_error',
        retryable=status_code in RETRYABLE_STATUS_CODES,
        retry_after_seconds=parse_retry_after(headers),
    )


def exception_error(exc: Exception, *, prefix: str = 'Provider') -> StreamDelta:
    """Convert transport exceptions without relying on message matching."""
    if isinstance(exc, httpx.TimeoutException):
        code = 'timeout'
        retryable = True
    elif isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.WriteError,
                          httpx.RemoteProtocolError, httpx.NetworkError)):
        code = 'connection_error'
        retryable = True
    else:
        code = 'provider_error'
        retryable = False
    return StreamDelta(
        type='error',
        error=f'{prefix} error: {exc}',
        error_code=code,
        retryable=retryable,
    )


__all__ = [
    'RETRYABLE_STATUS_CODES',
    'exception_error',
    'http_error',
    'parse_retry_after',
]
