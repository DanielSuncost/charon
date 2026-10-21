"""Opt-in, secret-free reporting of provider request configuration."""
from __future__ import annotations

import json
import os
import sys
from typing import Any


def _enabled(name: str) -> bool:
    return str(os.environ.get(name) or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def observe_provider_error(
    *, provider: str, model: str, status_code: int, raw_body: str,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Persist the complete response, including nested upstream errors.

    Never record request headers. Redact the credential if a server echoes it.
    The benchmark's existing telemetry switch also writes this event to its
    captured stderr, surviving deletion of the container's runtime state.
    """
    from charon.infra.diagnostics import record

    if api_key:
        raw_body = raw_body.replace(api_key, '[REDACTED]')
    event = {
        'provider': provider, 'model': model,
        'status_code': status_code, 'raw_body': raw_body,
    }
    record('provider', 'HTTP response error', **event)
    if _enabled('CHARON_EMIT_REQUEST_CONFIG'):
        print(json.dumps({'provider_error': event}, separators=(',', ':')),
              file=sys.stderr, flush=True)
    return event


def observe_reasoning_request(
    *,
    provider: str,
    model: str,
    requested: str,
    wire_field: str | None,
    wire_value: str | None,
) -> dict[str, Any]:
    """Validate and optionally emit the reasoning setting put on the wire.

    The JSON event contains only provider/model/configuration fields. It is
    safe for benchmark logs and intentionally excludes headers, credentials,
    messages, and request bodies.
    """
    field_emitted = bool(wire_field and wire_value is not None)
    if _enabled('CHARON_REQUIRE_REASONING_EFFORT') and not field_emitted:
        raise ValueError(
            f'{provider} did not emit a reasoning-effort field for requested level {requested!r}'
        )

    event = {
        'provider': provider,
        'model': model,
        'reasoning_effort_requested': requested,
        'reasoning_field': wire_field,
        'reasoning_effort_emitted': wire_value,
        'reasoning_field_emitted': field_emitted,
    }
    if _enabled('CHARON_EMIT_REQUEST_CONFIG'):
        print(
            json.dumps({'provider_request': event}, separators=(',', ':')),
            file=sys.stderr,
            flush=True,
        )
    return event
