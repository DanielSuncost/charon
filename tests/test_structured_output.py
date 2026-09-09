"""generate_structured() — schema-constrained generation for any caller
(internal subsystem, graph/workflow node, user-defined tool), not just tool
calls. Exercised via httpx.MockTransport, same pattern as
test_httpx_openai_provider.py — no live model calls.

Covers: a clean first-try pass, a case actually repaired via the reused
constrained-decoding retry, and the case that matters most — invalid output
that survives every bounded repair attempt gets rejected (ok=False) rather
than silently accepted, with the failure persisted so it can be reviewed
later via read_failures().
"""
import json

import httpx
import pytest

from charon.providers import structured_output as so
from charon.providers.httpx_openai import HttpxOpenAIProvider
from charon.providers.structured_output import (
    StructuredResult,
    generate_structured,
    read_failures,
    schema_validation_errors,
)

PERSON_SCHEMA = {
    'type': 'object',
    'properties': {'name': {'type': 'string'}, 'age': {'type': 'integer'}},
    'required': ['name', 'age'],
}


def _completion(obj) -> httpx.Response:
    return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(obj)}}]})


def _provider(handler) -> HttpxOpenAIProvider:
    provider = HttpxOpenAIProvider(base_url='http://fake-local:1234/v1', api_key='x')
    provider._mock_handler = handler
    return provider


# ---------------------------------------------------------------------------
# generate_structured: end to end against a mocked OpenAI-compatible server
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_structured_valid_output_on_first_try():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body['response_format'] == {
            'type': 'json_schema',
            'json_schema': {'name': 'person', 'schema': PERSON_SCHEMA, 'strict': True},
        }
        return _completion({'name': 'Ada', 'age': 30})

    result = await generate_structured(
        _provider(handler), PERSON_SCHEMA, prompt='describe Ada', model_id='m', schema_name='person',
    )

    assert isinstance(result, StructuredResult)
    assert result.ok is True
    assert result.data == {'name': 'Ada', 'age': 30}
    assert result.attempts == 1
    assert result.errors == []
    assert result.diagnostic_id is None


@pytest.mark.asyncio
async def test_generate_structured_repairs_missing_field_via_constrained_retry():
    calls = {'n': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        if calls['n'] == 1:
            return _completion({'name': 'Ada'})  # missing required 'age'
        body = json.loads(request.content)
        assert 'age' in body['messages'][1]['content'], 'repair retry must surface the specific validation error'
        return _completion({'name': 'Ada', 'age': 30})

    result = await generate_structured(
        _provider(handler), PERSON_SCHEMA, prompt='describe Ada', model_id='m', max_repair_attempts=2,
    )

    assert result.ok is True
    assert result.data == {'name': 'Ada', 'age': 30}
    assert result.attempts == 2
    assert calls['n'] == 2


@pytest.mark.asyncio
async def test_generate_structured_persists_invalid_output_as_diagnostic_when_repair_exhausted(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return _completion({'name': 'Ada'})  # always missing 'age', never recoverable here

    result = await generate_structured(
        _provider(handler), PERSON_SCHEMA, prompt='describe Ada', model_id='m',
        schema_name='person', max_repair_attempts=2, state_dir=tmp_path,
    )

    assert result.ok is False
    assert result.data == {'name': 'Ada'}
    assert result.attempts == 3  # 1 initial + 2 bounded repair rounds, never silently accepted
    assert any('age' in e for e in result.errors)
    assert result.diagnostic_id

    failures = read_failures(tmp_path)
    assert len(failures) == 1
    persisted = failures[0]
    assert persisted['diagnostic_id'] == result.diagnostic_id
    assert persisted['schema_name'] == 'person'
    assert persisted['attempts'] == 3
    assert any('age' in e for e in persisted['errors'])
    assert json.loads(persisted['raw_output']) == {'name': 'Ada'}


@pytest.mark.asyncio
async def test_generate_structured_max_repair_attempts_zero_skips_repair_rounds(tmp_path):
    calls = {'n': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        return _completion({'name': 'Ada'})  # missing 'age'

    result = await generate_structured(
        _provider(handler), PERSON_SCHEMA, prompt='describe Ada', model_id='m',
        max_repair_attempts=0, state_dir=tmp_path,
    )

    assert result.ok is False
    assert result.attempts == 1
    assert calls['n'] == 1, 'max_repair_attempts=0 must not make any repair round-trip'


@pytest.mark.asyncio
async def test_generate_structured_stops_early_when_repair_attempt_itself_fails(tmp_path):
    """The server rejects response_format on the retry (older llama.cpp/
    Ollama) — bounded repair must stop rather than keep burning attempts
    on a mechanism that clearly isn't supported, and still resolve to a
    clean rejection rather than raising."""
    calls = {'n': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        if calls['n'] == 1:
            return _completion({'name': 'Ada'})  # missing 'age'
        return httpx.Response(400, json={'error': {'message': 'response_format not supported'}})

    result = await generate_structured(
        _provider(handler), PERSON_SCHEMA, prompt='describe Ada', model_id='m',
        max_repair_attempts=3, state_dir=tmp_path,
    )

    assert result.ok is False
    assert calls['n'] == 2, 'must stop after the first failed repair attempt, not retry 3 more times'


@pytest.mark.asyncio
async def test_generate_structured_initial_request_failure_falls_through_to_repair_path(tmp_path):
    """Even the very first request can fail outright (connection reset,
    500, etc.) — that must not raise out of generate_structured; it flows
    into the same bounded repair path as a malformed completion would."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={'error': {'message': 'boom'}})

    result = await generate_structured(
        _provider(handler), PERSON_SCHEMA, prompt='describe Ada', model_id='m',
        max_repair_attempts=1, state_dir=tmp_path,
    )

    assert result.ok is False
    assert result.raw_text == ''
    assert result.diagnostic_id


@pytest.mark.asyncio
async def test_generate_structured_rejects_negative_max_repair_attempts():
    provider = _provider(lambda request: _completion({}))
    with pytest.raises(ValueError):
        await generate_structured(provider, PERSON_SCHEMA, prompt='x', model_id='m', max_repair_attempts=-1)


# ---------------------------------------------------------------------------
# schema_validation_errors: real JSON-Schema conformance, not just "is it a dict"
# ---------------------------------------------------------------------------

def test_schema_validation_errors_empty_for_conforming_data():
    assert schema_validation_errors({'name': 'Ada', 'age': 30}, PERSON_SCHEMA) == []


def test_schema_validation_errors_reports_missing_required_field():
    errors = schema_validation_errors({'name': 'Ada'}, PERSON_SCHEMA)
    assert any('age' in e for e in errors)


def test_schema_validation_errors_reports_wrong_type():
    errors = schema_validation_errors({'name': 'Ada', 'age': 'thirty'}, PERSON_SCHEMA)
    assert errors


def test_schema_validation_errors_non_dict_output_is_invalid():
    assert schema_validation_errors(['not', 'a', 'dict'], PERSON_SCHEMA) != []
    assert schema_validation_errors(None, PERSON_SCHEMA) != []


def test_schema_validation_errors_falls_back_to_required_key_check_without_jsonschema(monkeypatch):
    monkeypatch.setattr(so, '_HAS_JSONSCHEMA', False)
    assert any('age' in e for e in schema_validation_errors({'name': 'Ada'}, PERSON_SCHEMA))
    # the structural fallback only checks required keys are present, not their types
    assert schema_validation_errors({'name': 'Ada', 'age': 'not-an-int'}, PERSON_SCHEMA) == []


# ---------------------------------------------------------------------------
# read_failures
# ---------------------------------------------------------------------------

def test_read_failures_empty_when_none_persisted(tmp_path):
    assert read_failures(tmp_path) == []


def test_read_failures_ignores_unrelated_diagnostics_components(tmp_path):
    from charon.infra import diagnostics
    diagnostics.record('some_other_component', 'unrelated event', state_dir=tmp_path)
    assert read_failures(tmp_path) == []
