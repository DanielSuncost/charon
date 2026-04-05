import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'tui' / 'opentui'))

import chat_backend
from chat_backend import ChatBackend


def test_automate_list_commands_in_catalog():
    backend = ChatBackend()
    cmds = [item['cmd'] for item in backend._command_catalog()]
    assert '/automate list' in cmds
    assert '/automate list cron' in cmds
    assert '/automate list continuous' in cmds


def test_automate_list_filtered_outputs(monkeypatch, tmp_path):
    backend = ChatBackend()
    emitted = []

    monkeypatch.setattr(chat_backend, 'emit', lambda event: emitted.append(event))
    monkeypatch.setattr(chat_backend, 'STATE_DIR', tmp_path / 'state')

    from automation_runtime import create_automation

    project_root = tmp_path / 'project'
    project_root.mkdir()

    cron_doc = create_automation(
        chat_backend.STATE_DIR,
        project_root,
        title='Weekday check',
        goal='weekday cron check',
        kind='http_check',
        mode='scheduled',
        schedule={'type': 'cron', 'cron': '0 9 * * 1-5'},
        action={'url': 'https://example.com'},
    )
    cont_doc = create_automation(
        chat_backend.STATE_DIR,
        project_root,
        title='Always-on check',
        goal='continuous check',
        kind='http_check',
        mode='continuous',
        schedule={'type': 'continuous', 'poll_seconds': 30},
        action={'url': 'https://example.com'},
    )

    backend.handle_command('/automate list cron', 'req-list')
    status_events = [e for e in emitted if e.get('type') == 'status']
    assert status_events
    msg = status_events[-1]['message']
    assert cron_doc['automation_id'] in msg
    assert cont_doc['automation_id'] not in msg

    emitted.clear()
    backend.handle_command('/automate list continuous', 'req-list-2')
    status_events = [e for e in emitted if e.get('type') == 'status']
    msg = status_events[-1]['message']
    assert cont_doc['automation_id'] in msg
    assert cron_doc['automation_id'] not in msg
