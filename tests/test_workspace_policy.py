"""Overseer send/spawn policy — the same decisions and messages as Acheron's policy.js."""
from __future__ import annotations

from charon.workspace import evaluate_send_policy, evaluate_spawn_policy, looks_like_approval, redact


def _send(**facts):
    base = {'action': 'dispatch', 'paused': False, 'is_self': False, 'wired': True, 'target_status': 'idle',
            'user_typed_ago_ms': None, 'content': 'do the thing', 'force': False, 'forward_approvals': False, 'callsign': 'Builder'}
    base.update(facts)
    return evaluate_send_policy(**base)


def test_send_allowed_by_default():
    assert _send() == {'ok': True}


def test_paused_denies_everything():
    r = _send(paused=True, action='label')
    assert r['ok'] is False and r['code'] == 'paused' and 'PAUSED' in r['reason']


def test_self_and_unwired_targets():
    assert _send(is_self=True)['code'] == 'self'
    r = _send(wired=False)
    assert r['code'] == 'write_requires_wire' and 'Builder is not wired' in r['reason']


def test_label_and_interrupt_skip_send_checks():
    assert _send(action='label', target_status='error') == {'ok': True}
    assert _send(action='interrupt', user_typed_ago_ms=100) == {'ok': True}


def test_error_and_detached_targets():
    r = _send(target_status='error')
    assert r['code'] == 'no_error_targets' and 'force:true' in r['reason']
    assert _send(target_status='error', force=True) == {'ok': True}
    assert _send(target_status='detached', force=True)['code'] == 'no_detached_targets'


def test_busy_within_ten_seconds():
    r = _send(user_typed_ago_ms=9_999)
    assert r['code'] == 'busy' and 'typing in Builder' in r['reason']
    assert _send(user_typed_ago_ms=10_000) == {'ok': True}
    assert _send(user_typed_ago_ms=None) == {'ok': True}


def test_forwarding_approvals_into_waiting_sessions():
    for text in ('y', 'yes', 'n', 'No', '1', 'ok', '', ' a '):
        r = _send(target_status='waiting', content=text, action='intervene')
        assert r['code'] == 'forward_approvals', text
    assert _send(target_status='waiting', content='y', forward_approvals=True) == {'ok': True}
    assert _send(target_status='waiting', content='Here is the schema you asked for: {"id": "string"}') == {'ok': True}
    assert _send(target_status='idle', content='y') == {'ok': True}


def test_looks_like_approval():
    assert looks_like_approval('y') and looks_like_approval('YES') and looks_like_approval('2') and looks_like_approval('')
    assert not looks_like_approval('yes please') and not looks_like_approval('continue with the plan')


def test_redact_masks_secrets():
    text = 'token sk-ant-abcdefghijklmnopqrstuvwxyz and Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123 and AKIAABCDEFGHIJKLMNOP'
    out = redact(text)
    assert 'abcdefghijklmnopqrstuvwxyz' not in out and out.count('[redacted]') == 3
    assert redact(None) == '' and redact('plain text') == 'plain text'
    key = '-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----'
    assert 'MIIE' not in redact(key)


def test_spawn_policy():
    assert evaluate_spawn_policy(policy='allow', session_count=3, max_blocks=8) == {'ok': True, 'confirm': False}
    assert evaluate_spawn_policy(policy='confirm', session_count=3, max_blocks=8) == {'ok': True, 'confirm': True}
    r = evaluate_spawn_policy(policy='deny', session_count=0, max_blocks=8)
    assert r['ok'] is False and r['code'] == 'spawn_denied'
    r = evaluate_spawn_policy(policy='allow', session_count=8, max_blocks=8)
    assert r['code'] == 'spawn_cap' and 'cap 8' in r['reason']
