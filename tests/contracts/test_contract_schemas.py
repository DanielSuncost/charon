from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / 'docs' / 'contracts'
FIXTURES_DIR = Path(__file__).resolve().parent / 'fixtures'


class SchemaValidationError(AssertionError):
    pass


def _type_matches(value, expected: str) -> bool:
    if expected == 'object':
        return isinstance(value, dict)
    if expected == 'array':
        return isinstance(value, list)
    if expected == 'string':
        return isinstance(value, str)
    if expected == 'integer':
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == 'number':
        return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)
    if expected == 'boolean':
        return isinstance(value, bool)
    if expected == 'null':
        return value is None
    return False


def _validate(schema: dict, value, path: str = '$') -> None:
    expected_type = schema.get('type')
    if expected_type is not None:
        allowed = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_type_matches(value, t) for t in allowed):
            raise SchemaValidationError(f'{path}: expected type {allowed}, got {type(value).__name__}')

    if 'const' in schema and value != schema['const']:
        raise SchemaValidationError(f"{path}: expected const {schema['const']!r}, got {value!r}")

    if 'enum' in schema and value not in schema['enum']:
        raise SchemaValidationError(f"{path}: expected one of {schema['enum']}, got {value!r}")

    if isinstance(value, str):
        min_length = schema.get('minLength')
        if min_length is not None and len(value) < min_length:
            raise SchemaValidationError(f'{path}: string shorter than minLength={min_length}')
        pattern = schema.get('pattern')
        if pattern and not re.match(pattern, value):
            raise SchemaValidationError(f'{path}: string does not match pattern {pattern!r}')

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get('minimum')
        if minimum is not None and value < minimum:
            raise SchemaValidationError(f'{path}: number less than minimum={minimum}')

    if isinstance(value, list):
        min_items = schema.get('minItems')
        if min_items is not None and len(value) < min_items:
            raise SchemaValidationError(f'{path}: array shorter than minItems={min_items}')
        if schema.get('uniqueItems') and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            raise SchemaValidationError(f'{path}: array items must be unique')
        item_schema = schema.get('items')
        if item_schema:
            for idx, item in enumerate(value):
                _validate(item_schema, item, f'{path}[{idx}]')

    if isinstance(value, dict):
        required = schema.get('required', [])
        for key in required:
            if key not in value:
                raise SchemaValidationError(f"{path}: missing required key '{key}'")

        properties = schema.get('properties', {})
        additional = schema.get('additionalProperties', True)
        if additional is False:
            extra = set(value.keys()) - set(properties.keys())
            if extra:
                raise SchemaValidationError(f"{path}: additional properties not allowed: {sorted(extra)}")

        for key, prop_schema in properties.items():
            if key in value:
                _validate(prop_schema, value[key], f'{path}.{key}')


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_schema_files_exist_and_parse() -> None:
    names = {
        'agent.schema.json',
        'task.schema.json',
        'event.schema.json',
        'node-link.schema.json',
        'rlm-node.schema.json',
    }
    found = {p.name for p in SCHEMA_DIR.glob('*.schema.json')}
    assert names <= found

    for name in sorted(names):
        schema = _load_json(SCHEMA_DIR / name)
        assert schema['type'] == 'object'
        assert schema.get('required')


def test_valid_fixtures_match_schemas() -> None:
    valid_dir = FIXTURES_DIR / 'valid'
    for fixture in sorted(valid_dir.glob('*.json')):
        schema_name = fixture.name
        schema = _load_json(SCHEMA_DIR / schema_name)
        payload = _load_json(fixture)
        _validate(schema, payload)


def test_invalid_fixtures_fail_validation() -> None:
    invalid_dir = FIXTURES_DIR / 'invalid'
    for fixture in sorted(invalid_dir.glob('*.json')):
        schema_name = fixture.name
        schema = _load_json(SCHEMA_DIR / schema_name)
        payload = _load_json(fixture)
        try:
            _validate(schema, payload)
        except SchemaValidationError:
            continue
        raise AssertionError(f'Expected invalid fixture to fail: {fixture.name}')
