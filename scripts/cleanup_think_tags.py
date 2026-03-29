#!/usr/bin/env python3
"""Clean leaked <think> tags from saved Charon conversations.

Usage:
  python3 scripts/cleanup_think_tags.py
  python3 scripts/cleanup_think_tags.py --state-dir .charon_state
  python3 scripts/cleanup_think_tags.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def clean_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'(?im)^\s*</?think>\s*$', '', text)
    text = text.replace('<think>', '').replace('</think>', '')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def process_file(path: Path, dry_run: bool) -> tuple[int, bool]:
    changed = False
    fixed = 0
    out_lines: list[str] = []
    for raw in path.read_text().splitlines():
        if not raw.strip():
            out_lines.append(raw)
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            out_lines.append(raw)
            continue
        before = msg.get('content')
        after = clean_text(before) if isinstance(before, str) else before
        if after != before:
            msg['content'] = after
            changed = True
            fixed += 1
        out_lines.append(json.dumps(msg, ensure_ascii=False))
    if changed and not dry_run:
        path.write_text('\n'.join(out_lines) + '\n')
    return fixed, changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--state-dir', default='.charon_state')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    conv_dir = Path(args.state_dir) / 'conversations'
    if not conv_dir.exists():
        print(f'No conversations dir: {conv_dir}')
        return 0

    files = sorted(conv_dir.glob('*.jsonl'))
    total_files = 0
    total_msgs = 0
    for path in files:
        fixed, changed = process_file(path, args.dry_run)
        if changed:
            total_files += 1
            total_msgs += fixed
            mode = 'would clean' if args.dry_run else 'cleaned'
            print(f'{mode}: {path} ({fixed} messages)')

    if total_files == 0:
        print('No leaked <think> tags found.')
    else:
        mode = 'Would clean' if args.dry_run else 'Cleaned'
        print(f'{mode} {total_msgs} messages across {total_files} files.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
