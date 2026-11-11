#!/usr/bin/env python3
"""Count prompt-cache rewrites caused by compaction over a long session.

Every compaction replaces the oldest history with a summary, which changes
the prompt prefix and forces a full prompt-cache rewrite. This drives the
real ContextCompactor (engine configuration: 0.7 trigger, 20-message fresh
tail, 20k-token leaf chunks) turn by turn against an in-memory context store
and a fake summarizer, and reports rewrites per 100 turns.

Usage:
  measure_compaction_rewrites.py [--band 0.15] [--turns 200]
                                 [--budget 200000] [--turn-chars 6000]
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from charon.context.context_compactor import CompactionConfig, ContextCompactor  # noqa: E402
from charon.context.context_store import ContextStore  # noqa: E402
from charon.providers import Message, ModelInfo, StreamDelta  # noqa: E402


class _MemoryDB:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:')

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def executemany(self, sql, params_seq):
        return self.conn.executemany(sql, params_seq)

    def commit(self):
        self.conn.commit()

    def fetchone(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        row = cur.fetchone()
        return None if row is None else dict(zip([d[0] for d in cur.description], row, strict=False))

    def fetchall(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


class _Summarizer:
    calls = 0

    async def stream(self, messages, model, system_prompt, tools=None, thinking_level='off', max_tokens=16384):
        self.calls += 1
        yield StreamDelta(type='text', text='Summary: ' + 'detail ' * 150)
        yield StreamDelta(type='done', text='{}')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--band', type=float, default=CompactionConfig().hysteresis)
    ap.add_argument('--turns', type=int, default=200)
    ap.add_argument('--budget', type=int, default=200_000)
    ap.add_argument('--turn-chars', type=int, default=6000, help='user+assistant chars added per turn')
    args = ap.parse_args()

    db = _MemoryDB()
    ContextStore.ensure_schema(db)
    compactor = ContextCompactor(CompactionConfig(context_threshold=0.7, fresh_tail_count=20, hysteresis=args.band))
    summarizer = _Summarizer()
    model = ModelInfo(provider='sim', model_id='sim', context_window=args.budget)
    user_chars = args.turn_chars * 2 // 3
    compacted, sizes = [], []
    for turn in range(args.turns):
        ContextStore.persist_message(db, 'sim', Message(role='user', content=f'turn {turn} ' + 'u' * user_chars))
        ContextStore.persist_message(db, 'sim', Message(role='assistant', content='a' * (args.turn_chars - user_chars)))
        result = asyncio.run(compactor.evaluate_and_compact(
            db, 'sim', token_budget=args.budget, provider=summarizer, model=model))
        if result.action_taken:
            compacted.append(turn)
        sizes.append(ContextStore.get_context_token_count(db, 'sim'))

    print(f'band={args.band} turns={args.turns} budget={args.budget:,} compactions(=cache rewrites)={len(compacted)}')
    if compacted:
        span = args.turns - compacted[0]
        print(f'  rewrites per 100 turns after the first: {100 * len(compacted) / span:.1f}')
        print(f'  turns between rewrites: {[b - a for a, b in zip(compacted, compacted[1:], strict=False)]}')
        after = sizes[compacted[0]:]
        print(f'  context after first compaction: min {min(after):,}, max {max(after):,} tokens')
    print(f'  summarizer calls: {summarizer.calls}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
