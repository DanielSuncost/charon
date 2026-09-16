#!/usr/bin/env python3
"""Render or serve Charon's standalone system visualization."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from charon.viz import main  # noqa: E402

if __name__ == '__main__':
    raise SystemExit(main())
