import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'tui' / 'opentui'))

from chat_backend import ChatBackend, _parse_interval_phrase, _natural_language_to_cron


def test_parse_interval_phrase():
    assert _parse_interval_phrase('every hour') == 3600
    assert _parse_interval_phrase('every 2 hours') == 7200
    assert _parse_interval_phrase('daily') == 86400


def test_natural_language_to_cron():
    assert _natural_language_to_cron('every day at 9am check https://example.com') == '0 9 * * *'
    assert _natural_language_to_cron('every weekday at 8:30am check https://example.com') == '30 8 * * 1-5'


def test_natural_language_monitor_routes_to_monitor_command(monkeypatch):
    backend = ChatBackend()
    captured = {}

    def fake_handle_command(command, request_id):
        captured['command'] = command
        captured['request_id'] = request_id

    monkeypatch.setattr(backend, 'handle_command', fake_handle_command)
    backend.handle_chat('every hour check https://example.com and report if it breaks', 'req-2')

    assert captured['request_id'] == 'req-2'
    assert captured['command'] == '/monitor every hour check https://example.com'


def test_monitor_browser_command_routes_to_browser_automate(monkeypatch):
    backend = ChatBackend()
    captured = {}

    def fake_handle_command(command, request_id):
        captured['command'] = command
        captured['request_id'] = request_id

    monkeypatch.setattr(backend, 'handle_command', fake_handle_command)
    ChatBackend.handle_command(backend, '/monitor browser every hour https://example.com expect "Example Domain"', 'req-browser')

    assert captured['request_id'] == 'req-browser'
    assert captured['command'] == '/automate browser every 3600 seconds check https://example.com expect "Example Domain"'


def test_natural_language_cron_and_continuous_routes(monkeypatch):
    backend = ChatBackend()
    captured = []

    def fake_handle_command(command, request_id):
        captured.append((command, request_id))

    monkeypatch.setattr(backend, 'handle_command', fake_handle_command)
    backend.handle_chat('every weekday at 8:30am check https://example.com', 'req-3')
    backend.handle_chat('continuously check https://example.com', 'req-4')

    assert captured[0][0] == '/automate cron "30 8 * * 1-5" check https://example.com'
    assert captured[1][0] == '/automate continuous check https://example.com'


def test_automate_commands_listed():
    backend = ChatBackend()
    cmds = [item['cmd'] for item in backend._command_catalog()]
    assert '/monitor every hour <url>' in cmds
    assert '/automate status <automation_id>' in cmds
    assert '/automate cron "0 9 * * 1-5" check <url>' in cmds
    assert '/automate continuous every <n> seconds check <url>' in cmds
    assert '/monitor browser every hour <url> expect "text"' in cmds
