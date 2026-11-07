import sys

import pytest

from charon.computer import helper as hp
from charon.computer import permissions as perm

APP = hp.default_app_path()
BUILT = sys.platform == 'darwin' and (APP / hp.EXECUTABLE).exists()


def test_missing_helper_means_nothing_granted(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setenv('CHARON_DESKTOP_APP', str(tmp_path / 'Nope.app'))
    r = perm.probe()
    assert r['ready'] is False and r['missing'] == list(perm.GRANTS)
    assert 'not built' in '\n'.join(r['message'])


def test_adhoc_or_wrong_identity_is_refused(tmp_path, monkeypatch):
    app = tmp_path / 'CharonDesktop.app'
    (app / 'Contents/MacOS').mkdir(parents=True)
    (app / hp.EXECUTABLE).write_text('')

    class R:
        def __init__(self, rc=0, err=''):
            self.returncode, self.stderr = rc, err

    def fake(desc):
        return lambda cmd, **kw: R(0, desc if '-dv' in cmd else '')

    monkeypatch.setattr(hp.subprocess, 'run', fake('Identifier=ai.charon.desktop\nSignature=adhoc\nTeamIdentifier=not set'))
    assert 'ad-hoc' in hp.verify(app)['error']
    monkeypatch.setattr(hp.subprocess, 'run', fake('Identifier=org.python.python\nTeamIdentifier=ABC'))
    assert 'not ai.charon.desktop' in hp.verify(app)['error']
    monkeypatch.setattr(hp.subprocess, 'run', fake('Identifier=ai.charon.desktop\nTeamIdentifier=ABC'))
    assert hp.verify(app)['error'] == ''
    monkeypatch.setenv('CHARON_DESKTOP_TEAM', 'XYZ')
    assert 'not the required XYZ' in hp.verify(app)['error']


def test_missing_grants_name_the_helper_not_python():
    r = {'supported': True, 'ready': False, 'error': '', 'missing': [perm.INPUT_MONITORING],
         'grants': {perm.SCREEN_RECORDING: True, perm.ACCESSIBILITY: True, perm.INPUT_MONITORING: None},
         'helper': {'app': '/x/CharonDesktop.app', 'team': 'T'}}
    text = '\n'.join(perm.explain(r))
    assert '✗ Input Monitoring' in text and '/x/CharonDesktop.app' in text
    assert 'not to Python' in text


@pytest.mark.skipif(not BUILT, reason='Charon Desktop helper not built')
def test_built_helper_is_self_responsible_and_answers():
    with hp.DesktopHelper() as h:
        assert h.identity['bundle_id'] == hp.BUNDLE_ID
        assert h.identity['is_self_responsible'] is True
        # The helper authenticated us from the socket's audit token.
        assert h.caller['pid'] == __import__('os').getpid()
        assert h.caller['cdhash']
        assert h.call('nope')['ok'] is False
        r = perm.probe(h)
    assert set(r['grants']) == set(perm.GRANTS)
    assert all(isinstance(v, bool) for v in r['grants'].values())


@pytest.mark.skipif(not BUILT or not __import__('os').path.exists('/usr/bin/python3'), reason='needs helper and a second interpreter')
def test_helper_refuses_an_untrusted_caller():
    import subprocess
    code = (
        'import sys; sys.path.insert(0, %r)\n'
        'from charon.computer.helper import DesktopHelper, HelperError\n'
        'try:\n    DesktopHelper(%r).start(); print("ACCEPTED")\n'
        'except HelperError as e:\n    print("REFUSED", e)\n'
    ) % (str(__import__('pathlib').Path(hp.__file__).parents[2]), str(APP))
    out = subprocess.run(['/usr/bin/python3', '-I', '-c', code], capture_output=True, text=True, timeout=60).stdout
    assert out.startswith('REFUSED') and 'caller refused' in out


@pytest.mark.skipif(not BUILT, reason='Charon Desktop helper not built')
def test_kill_switch_starts_tripped_and_will_not_arm_unproven():
    with hp.DesktopHelper() as h:
        ks = h.call('killswitch_status')['killswitch']
        assert ks['armed'] is False
        if ks['input_monitoring'] and ks['accessibility']:
            pytest.skip('grants present; arming is a manual check')
        armed = h.call('killswitch_arm')
        assert armed['ok'] is False and armed['killswitch']['armed'] is False
        # Nothing is posted while the switch is not armed.
        r = h.call('killswitch_selftest', seconds=1)
        assert r['completed'] == 0 and r['interrupted']


@pytest.mark.skipif(not BUILT, reason='Charon Desktop helper not built')
def test_kernel_refuses_without_asking_and_audits():
    with hp.DesktopHelper() as h:
        for target in ('com.apple.keychainaccess', 'com.apple.Terminal', 'ai.charon.desktop'):
            r = h.request_grant('desktop.input.text', target, 5)
            assert r['ok'] is False and 'never-allowed' in r['error']
        assert h.request_grant('desktop.run.shell', 'com.apple.Notes', 5)['ok'] is False
        assert h.narrow_grant('g-0', 1)['ok'] is False
        assert h.grants() == []
        v = h.verify_audit()
        assert v['audit']['ok'] is True, v
