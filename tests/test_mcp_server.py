"""charon.mcp_server — MCP stdio framing and tool exposure, driven as a subprocess."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
CONTRACT = ROOT / 'docs' / 'contracts' / 'overseer-tools.json'


def _run(lines: list[dict | str], *extra_args: str, cwd: Path | None = None, timeout: float = 60.0) -> list[dict]:
    """Feed request lines to a fresh server process; return the parsed response lines."""
    payload = '\n'.join(line if isinstance(line, str) else json.dumps(line) for line in lines) + '\n'
    env = dict(os.environ)
    env['PYTHONPATH'] = str(SRC) + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    env['CHARON_EMBED_BACKEND'] = 'local'
    proc = subprocess.run(
        [sys.executable, '-m', 'charon.mcp_server', *extra_args],
        input=payload, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(cwd or ROOT),
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))   # every stdout line must be a JSON-RPC message
    return out


INIT = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '0'}}}
INITIALIZED = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}


def test_initialize_handshake_shape(tmp_path):
    out = _run([INIT, INITIALIZED, {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}], '--project-root', str(tmp_path))
    assert len(out) == 2, out                      # the notification produced no reply
    init = out[0]
    assert init['id'] == 1 and init['jsonrpc'] == '2.0'
    r = init['result']
    assert r['protocolVersion'] == '2025-06-18'  # echoes the client's version
    assert r['capabilities'] == {'tools': {}}
    assert r['serverInfo']['name'] == 'charon'
    assert r['serverInfo']['version']
    assert out[1] == {'jsonrpc': '2.0', 'id': 2, 'result': {}}


def test_tools_list_profile_all_has_core_tools_with_inputSchema(tmp_path):
    out = _run([{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}], '--project-root', str(tmp_path))
    tools = out[0]['result']['tools']
    names = {t['name'] for t in tools}
    assert {'Read', 'Bash', 'Edit', 'Write'} <= names
    for t in tools:
        assert 'inputSchema' in t and 'input_schema' not in t
        assert isinstance(t['inputSchema'], dict)


def test_tools_list_profile_overseer_is_exactly_the_contract(tmp_path):
    contract = json.loads(CONTRACT.read_text())['tools']
    out = _run([{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}], '--project-root', str(tmp_path), '--profile', 'overseer')
    names = [t['name'] for t in out[0]['result']['tools']]
    assert names == [t['name'] for t in contract]
    assert len(names) == 20 and all(n.startswith('acheron_') for n in names)
    by_name = {t['name']: t for t in out[0]['result']['tools']}
    assert by_name['acheron_dispatch']['inputSchema']['required'] == ['work_item_id', 'session', 'prompt']


def test_tools_call_read_returns_file_content(tmp_path):
    target = tmp_path / 'hello.txt'
    target.write_text('hello from charon\n')
    out = _run([{'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {'name': 'Read', 'arguments': {'path': str(target)}}}],
               '--project-root', str(tmp_path))
    res = out[0]['result']
    assert res['isError'] is False
    assert res['content'][0]['type'] == 'text'
    assert 'hello from charon' in res['content'][0]['text']


def test_unknown_tool_and_unknown_method(tmp_path):
    out = _run([
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'Nope', 'arguments': {}}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'resources/list'},
    ], '--project-root', str(tmp_path))
    assert out[0]['error']['code'] == -32602
    assert 'Nope' in out[0]['error']['message']
    assert out[1]['error']['code'] == -32601


def test_invalid_json_and_batch(tmp_path):
    out = _run(['{not json', '[{"jsonrpc":"2.0","id":9,"method":"ping"}]'], '--project-root', str(tmp_path))
    assert out[0]['error']['code'] == -32700 and out[0]['id'] is None
    assert out[1]['error']['code'] == -32600


def test_notifications_get_no_reply(tmp_path):
    out = _run([
        INITIALIZED,
        {'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 1}},
        {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 'Read', 'arguments': {'path': '/nonexistent'}}},  # no id → silent
        {'jsonrpc': '2.0', 'id': 3, 'method': 'ping'},
    ], '--project-root', str(tmp_path))
    assert [m['id'] for m in out] == [3]


def test_overseer_tool_without_executor_is_a_clean_error(tmp_path):
    out = _run([{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'acheron_fleet', 'arguments': {}}}],
               '--project-root', str(tmp_path), '--profile', 'overseer')
    msg = out[0]
    if 'error' in msg:
        pytest.fail(f'expected an isError result, got a protocol error: {msg}')
    res = msg['result']
    try:
        from charon.tools import TOOL_EXECUTORS  # noqa: WPS433
        installed = 'OverseerFleet' in TOOL_EXECUTORS
    except Exception:
        installed = False
    if installed:
        assert 'isError' in res     # executor exists on this checkout: any well-formed result is fine
    else:
        assert res['isError'] is True
        assert 'not available on this Charon' in res['content'][0]['text']


def test_tools_allowlist_hides_everything_else(tmp_path):
    out = _run([
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'Bash', 'arguments': {'command': 'echo hi'}}},
    ], '--project-root', str(tmp_path), '--tools', 'Read')
    assert [t['name'] for t in out[0]['result']['tools']] == ['Read']
    assert out[1]['error']['code'] == -32602


def test_approval_gate_is_denied_without_hanging(tmp_path):
    # `rm -rf` is a dangerous command → needs approval; with --approve none it must
    # come back as a Blocked isError result quickly instead of waiting 60 s.
    out = _run([{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                 'params': {'name': 'Bash', 'arguments': {'command': f'rm -rf {tmp_path}/never-created-dir'}}}],
               '--project-root', str(tmp_path), timeout=30)
    res = out[0]['result']
    assert res['isError'] is True
    assert 'Blocked' in res['content'][0]['text']


def test_stdout_is_only_jsonrpc(tmp_path):
    """Import-time chatter must never reach stdout (it would corrupt the stream)."""
    out = _run([{'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}], '--project-root', str(tmp_path))
    assert out == [{'jsonrpc': '2.0', 'id': 1, 'result': {}}]
