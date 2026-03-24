from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'apps' / 'tui' / 'layout_utils.py'

spec = importlib.util.spec_from_file_location('layout_utils', MODULE_PATH)
layout_utils = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = layout_utils
spec.loader.exec_module(layout_utils)
choose_mascot_layout = layout_utils.choose_mascot_layout


def test_hidden_layout_for_very_small_terminal():
    layout = choose_mascot_layout(70, 20)
    assert layout.variant == 'hidden'
    assert layout.max_lines == 0


def test_tiny_layout_for_medium_terminal():
    layout = choose_mascot_layout(100, 26)
    assert layout.variant == 'tiny'
    assert layout.max_lines >= 4


def test_full_layout_for_large_terminal():
    layout = choose_mascot_layout(140, 40)
    assert layout.variant == 'full'
    assert layout.max_lines >= 8
