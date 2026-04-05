import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'tui' / 'opentui'))

from chat_backend import ChatBackend


def test_root_command_suggestions_include_automation_commands():
    backend = ChatBackend()
    cmds = [item['cmd'] for item in backend._get_suggestions('/')]
    assert '/automate list' in cmds
    assert '/automate list cron' in cmds
    assert '/automate cron "0 9 * * 1-5" check <url>' in cmds
    assert '/automate continuous every <n> seconds check <url>' in cmds
    assert '/monitor every hour <url>' in cmds
    assert '/monitor browser every hour <url> expect "text"' in cmds


def test_automation_prefix_suggestions_include_new_commands():
    backend = ChatBackend()
    cmds = [item['cmd'] for item in backend._get_suggestions('/automate')]
    assert '/automate list' in cmds
    assert '/automate list continuous' in cmds
    assert '/automate browser every <n> <unit> check <url> expect "text"' in cmds
