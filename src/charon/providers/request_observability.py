"""Opt-in, secret-free reporting of provider request configuration."""
from __future__ import annotations

import json
import os
import sys
from typing import Any


def _enabled(name: str) -> bool:
    return str(os.environ.get(name) or '').strip().lower() in {'1', 'true', 'yes', 'on'}


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
