from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'apps' / 'tui' / 'message_style.py'

spec = importlib.util.spec_from_file_location('message_style', MODULE_PATH)
message_style = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = message_style
spec.loader.exec_module(message_style)


def test_shorten_status_keeps_first_sentence():
    s = message_style.shorten_status('patched renderer. running tests now. next line')
    assert s == 'patched renderer.'


def test_format_assistant_message_conversational_keeps_full_text():
    txt = 'Longer explanatory answer with details.'
    assert message_style.format_assistant_message(txt, message_type='conversational') == txt


def test_format_assistant_message_alert_is_shortened():
    out = message_style.format_assistant_message('This is a long alert message that should still be clipped for terse display in status mode', message_type='alert')
    assert len(out) <= 100
