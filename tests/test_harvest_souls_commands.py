from backend import common
from chat_backend import ChatBackend


def test_harvest_souls_status_routes_to_status_branch(monkeypatch, tmp_path):
    # Regression: the subcommand slice was command[16:] but '/harvest_souls '
    # is 15 chars, so 'status' parsed as 'tatus' and fell through to the
    # default full-scan branch.
    backend = ChatBackend()
    emitted = []

    monkeypatch.setattr(common, 'emit', lambda event: emitted.append(event))
    monkeypatch.setattr(common, 'STATE_DIR', tmp_path / 'state')

    backend.handle_command('/harvest_souls status', 'req-hs-status')

    messages = [e.get('message', '') for e in emitted if e.get('type') == 'status']
    assert any('No scan found' in m for m in messages)
    assert not any('Scanning agent repos' in m for m in messages)


def test_harvest_adopt_quarantines_corrupt_adopted_file(monkeypatch, tmp_path):
    """Regression: a corrupt adopted.json used to be silently replaced by the
    next adopt; it must be preserved as adopted.json.corrupt-<n>."""
    import json

    backend = ChatBackend()
    emitted = []

    state = tmp_path / 'state'
    abilities = state / 'assimilation' / 'abilities'
    abilities.mkdir(parents=True)
    (abilities / 'agentx.json').write_text(json.dumps({
        'analysis': [{'name': 'foo-ability', 'priority': 'high', 'charon_has': False}],
    }))
    adopted_file = state / 'assimilation' / 'adopted.json'
    adopted_file.write_text('[{"name": "old-ability"')  # truncated write

    monkeypatch.setattr(common, 'emit', lambda event: emitted.append(event))
    monkeypatch.setattr(common, 'STATE_DIR', state)

    backend._harvest_souls_adopt('1', None)

    # New adoption written fresh; the unreadable original is quarantined.
    assert [a['name'] for a in json.loads(adopted_file.read_text())] == ['foo-ability']
    quarantined = state / 'assimilation' / 'adopted.json.corrupt-0'
    assert quarantined.exists()
    assert 'old-ability' in quarantined.read_text()
