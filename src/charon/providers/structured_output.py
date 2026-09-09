"""General-purpose schema-constrained generation.

httpx_openai.py's _finalize_tool_call_arguments() (parse -> lenient
truncation repair -> one grammar-constrained retry) is reachable only from
inside HttpxOpenAIProvider.stream()'s tool-call loop, and each call is
constrained to exactly one tool's schema — there's no way for an internal
subsystem or a user-defined workflow to ask "give me output matching this
JSON Schema" directly. This module is that entry point.

It deliberately reuses that exact ladder rather than inventing a second
one: the first pass goes straight through _finalize_tool_call_arguments
unchanged (the same parse/lenient-repair/one-constrained-retry sequence
tool calls get). What's new here is real JSON-Schema validation on the
result — the tool-call path only ever checked "is this a dict", not
whether it actually conforms to the schema's required fields/types/etc.
When validation fails, up to max_repair_attempts further rounds of
_repair_arguments_via_constrained_decoding run (the same primitive,
called again), each one fed the previous candidate's specific validation
errors so the retry has something concrete to fix.

A caller (internal or user-defined) that exhausts every attempt gets
StructuredResult(ok=False, ...) — never a silently-accepted invalid
result — and the failure is persisted via charon.infra.diagnostics (the
existing "make otherwise-silent degradation inspectable" mechanism used
elsewhere in this same provider) so it can be reviewed or evaluated later.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from charon.infra import diagnostics
from charon.providers.httpx_openai import (
    HttpxOpenAIProvider,
    _diag,
    _finalize_tool_call_arguments,
    _repair_arguments_via_constrained_decoding,
)

try:
    import jsonschema
    _HAS_JSONSCHEMA = True
except Exception:  # pragma: no cover - environment without jsonschema
    _HAS_JSONSCHEMA = False

DEFAULT_SYSTEM_PROMPT = 'Output ONLY a JSON object matching the given schema — no prose, no code fences.'

# Tag on the diagnostics record so read_failures() can find these specifically
# among whatever else charon.infra.diagnostics accumulates.
_DIAG_COMPONENT = 'structured_generation'


@dataclass
class StructuredResult:
    """Outcome of generate_structured().

    ok=False means every repair attempt was exhausted: `data` (if present)
    is the last, still-invalid candidate — returned for inspection, not as
    something the caller should treat as conforming. Check `ok`, not just
    whether `data` is truthy.
    """
    ok: bool
    data: dict | None
    raw_text: str
    attempts: int
    errors: list[str] = field(default_factory=list)
    diagnostic_id: str | None = None


def schema_validation_errors(data: Any, schema: dict) -> list[str]:
    """Real JSON-Schema conformance errors — required fields, types, enum
    values, etc. — not just "is this parseable JSON", which is all the
    tool-call repair ladder ever checked. Degrades to a structural
    required-keys-only check when jsonschema isn't installed, mirroring
    workspace/bundle.validate_bundle's exact fallback convention."""
    if not isinstance(data, dict):
        return [f'output is {type(data).__name__}, expected an object']
    if not _HAS_JSONSCHEMA:
        return [
            f'$.{key}: missing (jsonschema not installed; only required-key checks ran)'
            for key in (schema.get('required') or []) if key not in data
        ]
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f'{".".join(str(p) for p in e.absolute_path) or "$"}: {e.message}'
        for e in validator.iter_errors(data)
    ]


async def _request_structured_completion(
    client: httpx.AsyncClient, base_url: str, headers: dict, model_id: str,
    schema_name: str, schema: dict, prompt: str, system_prompt: str,
) -> str:
    """One non-streaming, schema-constrained completion — the initial ask,
    using the same OpenAI-compatible response_format: json_schema field
    _repair_arguments_via_constrained_decoding uses for a repair retry.
    Never raises: an unreachable/unsupporting server yields '', which
    flows into the same repair path a malformed completion would."""
    body = {
        'model': model_id,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': prompt},
        ],
        'max_tokens': 4096,
        'stream': False,
        'response_format': {
            'type': 'json_schema',
            'json_schema': {'name': schema_name, 'schema': schema, 'strict': True},
        },
    }
    try:
        resp = await client.post(f'{base_url}/chat/completions', json=body, headers=headers, timeout=60.0)
        if resp.status_code != 200:
            return ''
        data = resp.json()
        return ((data.get('choices') or [{}])[0].get('message') or {}).get('content', '') or ''
    except Exception as e:
        _diag('structured_output', 'initial schema-constrained completion request failed', error=e, schema_name=schema_name)
        return ''


def _persist_failure(state_dir, *, schema_name: str, schema: dict, raw_output: str,
                      errors: list[str], attempts: int, model_id: str) -> dict:
    """charon.infra.diagnostics.record() already is the established
    "make silent degradation inspectable" mechanism (see its own
    docstring) — reused here, tagged with _DIAG_COMPONENT, rather than a
    second bespoke store."""
    diagnostic_id = uuid.uuid4().hex[:16]
    diagnostics.record(
        _DIAG_COMPONENT,
        f'structured output still failed schema validation after {attempts} attempt(s)',
        state_dir=state_dir,
        diagnostic_id=diagnostic_id,
        schema_name=schema_name,
        schema=schema,
        raw_output=raw_output[:4000],
        errors=errors,
        attempts=attempts,
        model_id=model_id,
    )
    return {
        'diagnostic_id': diagnostic_id, 'schema_name': schema_name, 'raw_output': raw_output,
        'errors': errors, 'attempts': attempts, 'model_id': model_id,
    }


def read_failures(state_dir, limit: int = 50) -> list[dict]:
    """Failed structured-generation attempts, newest last — for a human
    review pass, a held-out eval, or the prompt-evolution loop planned as
    a follow-up to this work. Reads a wider window of the shared
    diagnostics log than `limit` before filtering, since other
    components' entries interleave with these."""
    records = diagnostics.read_recent(state_dir, limit=max(limit, 200) * 4)
    matches = [r for r in records if r.get('component') == _DIAG_COMPONENT]
    return matches[-limit:] if limit else matches


async def generate_structured(
    provider: HttpxOpenAIProvider,
    schema: dict,
    *,
    prompt: str,
    model_id: str,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    schema_name: str = 'output',
    max_repair_attempts: int = 2,
    state_dir=None,
) -> StructuredResult:
    """Schema-constrained generation for any caller — an internal
    subsystem, a graph/workflow node, a user-defined tool — not only
    tool-call arguments.

    Reuses httpx_openai's exact repair ladder rather than a second one:
    the first pass goes through _finalize_tool_call_arguments unchanged
    (parse -> lenient repair -> one constrained retry). Real JSON-Schema
    validation then decides whether up to `max_repair_attempts` further
    rounds of _repair_arguments_via_constrained_decoding run — each fed
    the previous candidate plus its specific validation errors, so the
    model has something concrete to fix rather than just "try again".

    Never silently accepts invalid output: when every attempt is
    exhausted, the result comes back with ok=False and the failure (raw
    output, schema, validation errors) is persisted via
    charon.infra.diagnostics — see read_failures().
    """
    if max_repair_attempts < 0:
        raise ValueError('max_repair_attempts must be >= 0')

    transport = httpx.MockTransport(provider._mock_handler) if provider._mock_handler is not None else None
    client, _ = await provider._clients.get(transport=transport)
    headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {provider._api_key}'}
    base_url = provider._base_url

    raw_text = await _request_structured_completion(
        client, base_url, headers, model_id, schema_name, schema, prompt, system_prompt,
    )
    candidate = await _finalize_tool_call_arguments(
        raw_text, schema_name, schema, client=client, base_url=base_url, headers=headers, model_id=model_id,
    )
    errors = schema_validation_errors(candidate, schema)

    extra_rounds = 0
    while errors and extra_rounds < max_repair_attempts:
        extra_rounds += 1
        feedback = f'{json.dumps(candidate)}\n\nThis does not satisfy the schema: {"; ".join(errors)}'
        repaired = await _repair_arguments_via_constrained_decoding(
            client, base_url, headers, model_id, schema_name, schema, feedback,
        )
        if repaired is None:
            break  # server/model can't help further; stop burning attempts
        candidate = repaired
        errors = schema_validation_errors(candidate, schema)

    attempts = 1 + extra_rounds
    if errors:
        record = _persist_failure(
            state_dir, schema_name=schema_name, schema=schema, raw_output=json.dumps(candidate),
            errors=errors, attempts=attempts, model_id=model_id,
        )
        return StructuredResult(
            ok=False, data=candidate, raw_text=raw_text, attempts=attempts,
            errors=errors, diagnostic_id=record['diagnostic_id'],
        )
    return StructuredResult(ok=True, data=candidate, raw_text=raw_text, attempts=attempts, errors=[])
