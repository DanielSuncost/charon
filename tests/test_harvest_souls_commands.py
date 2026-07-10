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
