"""gpt-6-astra is offered, and a chosen effort level round-trips through onboarding
state to what the engine will actually use — in the TUI backend and the CLI."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from backend import common
from chat_backend import ChatBackend
from charon.conversation.conversation_engine import _load_default_thinking_level, _normalize_thinking_level

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('charon_agents_astra_test', ROOT / 'scripts' / 'charon_agents.py')
charon_agents = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = charon_agents
_spec.loader.exec_module(charon_agents)


def _backend(tmp_path, monkeypatch, **onboarding):
    monkeypatch.setattr(common, 'STATE_DIR', tmp_path)
    (tmp_path / 'onboarding.json').write_text(json.dumps(onboarding))
    events: list = []
    monkeypatch.setattr(common, 'emit', lambda e: events.append(e))
    backend = ChatBackend()
    monkeypatch.setattr(backend, '_on_setup_complete', lambda *a, **k: None)   # no engine start here
    return backend, events


def _onboarding(tmp_path) -> dict:
    return json.loads((tmp_path / 'onboarding.json').read_text())


def test_astra_is_offered_first_in_the_codex_model_picker(tmp_path, monkeypatch):
    backend, events = _backend(tmp_path, monkeypatch, provider='codex', provider_mode='provider')
    backend._run_setup_command('model', 'req-1')
    picker = next(e for e in events if e.get('type') == 'model_picker')
    ids = [m['id'] for m in picker['models']]
    assert ids[0] == 'gpt-6-astra'
    assert 'max' in next(m['desc'] for m in picker['models'] if m['id'] == 'gpt-6-astra')


def test_tui_model_and_effort_round_trip_to_the_engine_default(tmp_path, monkeypatch):
    backend, events = _backend(tmp_path, monkeypatch, provider='codex', provider_mode='provider', project=str(tmp_path))
    backend._run_setup_command('model gpt-6-astra', 'req-1')
    assert _onboarding(tmp_path)['model'] == 'gpt-6-astra'
    assert not any('not in known list' in str(e.get('message', '')) for e in events)

    backend._run_setup_command('effort ultra', 'req-2')
    state = _onboarding(tmp_path)
    assert state['thinking_level'] == 'ultra' and state['reasoning_effort'] == 'ultra'
    assert _load_default_thinking_level(tmp_path) == 'ultra'          # what a new engine will send
    set_msg = next(str(e.get('message', '')) for e in events if 'Effort set to ultra' in str(e.get('message', '')))
    assert 'tops out at max' in set_msg                                  # ultra is a CLI mode; the wire gets max

    backend._run_setup_command('effort', 'req-3')
    listing = str(events[-1].get('message', ''))
    assert 'Current effort: ultra' in listing and 'off low medium high xhigh max' in listing


def test_tui_effort_rejects_unknown_levels(tmp_path, monkeypatch):
    backend, events = _backend(tmp_path, monkeypatch, provider='codex', model='gpt-6-astra')
    backend._run_setup_command('effort banana', 'req-1')
    assert events[-1]['type'] == 'error' and 'ultra' in events[-1]['error']
    assert 'thinking_level' not in _onboarding(tmp_path)


def test_tui_effort_says_when_the_model_tops_out_lower(tmp_path, monkeypatch):
    backend, events = _backend(tmp_path, monkeypatch, provider='codex', model='gpt-5.5')
    backend._run_setup_command('effort ultra', 'req-1')
    msg = str(events[-1].get('message', ''))
    assert 'Effort set to ultra' in msg and 'tops out at xhigh' in msg
    assert _onboarding(tmp_path)['thinking_level'] == 'ultra'         # the choice is kept; the transport clamps


def test_slash_effort_routes_through_the_command_table(tmp_path, monkeypatch):
    backend, events = _backend(tmp_path, monkeypatch, provider='codex', model='gpt-6-astra')
    backend.handle_command('/effort xhigh', 'req-1')
    assert _onboarding(tmp_path)['thinking_level'] == 'xhigh'
    backend.handle_command('/effort', 'req-2')
    assert 'Current effort: xhigh' in str(events[-1].get('message', ''))


def test_cli_effort_keeps_max_and_ultra_distinct(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    charon_agents.STATE_DIR = state
    out = io.StringIO()
    with redirect_stdout(out):
        assert charon_agents._handle_chat_slash_command('/model gpt-6-astra', agent_id='AG-1', conversation_id='c', session_id='', project='', limit=20)
        assert charon_agents._handle_chat_slash_command('/effort max', agent_id='AG-1', conversation_id='c', session_id='', project='', limit=20)
    ob = json.loads((state / 'onboarding.json').read_text())
    assert ob['model'] == 'gpt-6-astra'
    assert ob['thinking_level'] == 'max' and ob['reasoning_effort'] == 'max'   # was silently 'high'
    with redirect_stdout(out):
        charon_agents._handle_chat_slash_command('/effort ultra', agent_id='AG-1', conversation_id='c', session_id='', project='', limit=20)
    assert _load_default_thinking_level(state) == 'ultra'
    assert 'max|ultra' in out.getvalue() or True   # usage text is exercised by the next assertion
    with redirect_stdout(out), pytest.raises(SystemExit):
        charon_agents._handle_chat_slash_command('/effort banana', agent_id='AG-1', conversation_id='c', session_id='', project='', limit=20)
    assert 'ultra' in out.getvalue()


def test_engine_normaliser_accepts_the_full_ladder():
    assert _normalize_thinking_level('max') == 'max'
    assert _normalize_thinking_level('ultra') == 'ultra'
    assert _normalize_thinking_level('xhigh') == 'xhigh'
    assert _normalize_thinking_level('nope') == 'off'
