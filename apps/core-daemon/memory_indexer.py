"""Memory indexer — indexes conversation turns into the semantic memory engine.

Called after each conversation session to build the searchable memory store.
This is a background operation — it never blocks the conversation.

Non-breaking: if sqlite-vec or sentence-transformers are not installed,
this module does nothing.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


def _index_in_background(state_dir: Path, turns: list[dict], agent_id: str, conv_id: str):
    """Index conversation turns into the memory engine. Runs in a thread."""
    try:
        from memory_engine import MemoryEngine

        engine = MemoryEngine(state_dir)
        for i, turn in enumerate(turns):
            content = turn.get('content', '')
            if isinstance(content, list):
                parts = [b.get('text', '') for b in content if isinstance(b, dict)]
                content = ' '.join(parts)
            if not isinstance(content, str) or len(content) < 30:
                continue

            # Index user and assistant turns (both contain useful info)
            engine.add(
                content[:2000],
                category='event',
                container_tag=agent_id,
                source_agent=agent_id,
                source_conv=conv_id,
                source_turn=i,
                check_updates=False,  # fast path — skip version detection during indexing
            )

        engine.close()
    except ImportError:
        pass  # sqlite-vec or sentence-transformers not installed
    except Exception:
        pass  # don't crash the main loop on indexing errors


def index_conversation(
    state_dir: Path,
    turns: list[dict],
    agent_id: str = "",
    conv_id: str = "",
) -> None:
    """Index conversation turns into the semantic memory engine.

    Non-blocking — spawns a background thread.
    Safe to call even if dependencies are missing.
    """
    if not turns:
        return

    thread = threading.Thread(
        target=_index_in_background,
        args=(state_dir, turns, agent_id, conv_id),
        daemon=True,
    )
    thread.start()


def index_conversation_sync(
    state_dir: Path,
    turns: list[dict],
    agent_id: str = "",
    conv_id: str = "",
) -> int:
    """Synchronous version — returns count of indexed memories. For testing."""
    try:
        from memory_engine import MemoryEngine

        engine = MemoryEngine(state_dir)
        count = 0
        for i, turn in enumerate(turns):
            content = turn.get('content', '')
            if isinstance(content, list):
                parts = [b.get('text', '') for b in content if isinstance(b, dict)]
                content = ' '.join(parts)
            if not isinstance(content, str) or len(content) < 30:
                continue

            engine.add(
                content[:2000],
                category='event',
                container_tag=agent_id,
                source_agent=agent_id,
                source_conv=conv_id,
                source_turn=i,
                check_updates=False,
            )
            count += 1

        engine.close()
        return count
    except ImportError:
        return 0
    except Exception:
        return 0
