import sys

import pytest

from charon.computer import permissions as perm


def _result(grants, **resp):
    r = {'supported': True, 'where': 'the persistent runner', 'grants': grants,
         'missing': [g for g in perm.GRANTS if grants.get(g) is not True],
         'responsible': {'name': 'Python', 'grant_target': '/x/Python.app', **resp}}
    r['ready'] = not r['missing']
    return r


def test_unknown_grant_state_counts_as_missing():
    r = _result({perm.SCREEN_RECORDING: True, perm.ACCESSIBILITY: None, perm.INPUT_MONITORING: False})
    assert r['missing'] == [perm.ACCESSIBILITY, perm.INPUT_MONITORING]
    text = '\n'.join(perm.explain(r))
    assert 'OFF until these are granted' in text
    assert '/x/Python.app' in text and 'the persistent runner' in text


def test_interpreter_and_adhoc_grants_are_called_out():
    r = _result({g: False for g in perm.GRANTS}, interpreter=True, adhoc=True)
    text = '\n'.join(perm.explain(r))
    assert 'every script it runs' in text
    assert 'drops it without warning' in text


def test_all_granted_names_the_holder():
    text = '\n'.join(perm.explain(_result({g: True for g in perm.GRANTS})))
    assert 'All granted to Python' in text


def test_preflight_failure_is_not_granted(monkeypatch):
    monkeypatch.setattr(perm, '_preflight', lambda: {g: None for g in perm.GRANTS})
    monkeypatch.setattr(perm, 'responsible_process', lambda: {'name': 'X', 'grant_target': '/X'})
    monkeypatch.setattr(sys, 'platform', 'darwin')
    r = perm.probe(where='test')
    assert r['ready'] is False and r['missing'] == list(perm.GRANTS)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS only')
def test_responsible_process_resolves_a_real_binary():
    resp = perm.responsible_process()
    assert resp['responsible_pid'] > 0
    assert resp['executable']
