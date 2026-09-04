"""Low-overhead hot-path timing for interactive and agent turns."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from charon.infra import config


_write_lock = threading.Lock()


@dataclass
class TurnTimer:
    """Monotonic milestones for one submitted user turn."""

    started_ns: int = field(default_factory=time.monotonic_ns)
    marks_ns: dict[str, int] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)

    def mark(self, name: str) -> float:
        now = time.monotonic_ns()
        self.marks_ns[name] = now
        return round((now - self.started_ns) / 1_000_000.0, 3)

    def set(self, **values: Any) -> None:
        self.values.update(values)

    def elapsed_ms(self) -> float:
        return round((time.monotonic_ns() - self.started_ns) / 1_000_000.0, 3)

    def snapshot(self) -> dict[str, Any]:
        metrics = {
            f'{name}_ms': round((value - self.started_ns) / 1_000_000.0, 3)
            for name, value in self.marks_ns.items()
        }
        metrics.update(self.values)
        metrics['elapsed_ms'] = self.elapsed_ms()
        return metrics


def persist_turn_metrics(
    state_dir: str | Path | None,
    metrics: dict[str, Any],
) -> None:
    """Append an opt-in timing record. Never raises or writes protocol streams."""
    if not config.performance_timing() or state_dir is None:
        return
    try:
        directory = Path(state_dir) / 'performance'
        directory.mkdir(parents=True, exist_ok=True)
        row = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            **metrics,
        }
        with _write_lock:
            with (directory / 'turns.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    except Exception:
        pass


__all__ = ['TurnTimer', 'persist_turn_metrics']
