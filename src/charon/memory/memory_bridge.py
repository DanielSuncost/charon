"""Memory bridge — import memory from external agent frameworks.

Currently supports:
  - Hermes Agent: MEMORY.md, USER.md (§-delimited), state.db (sessions + FTS5)

Usage:
    from charon.memory.memory_bridge import import_hermes
    stats = import_hermes(charon_state_dir)
    # stats = {"memory_entries": 12, "user_entries": 8, "messages": 350}
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
ENTRY_DELIMITER = "\n§\n"


def _parse_section_entries(text: str) -> list[str]:
    """Parse §-delimited entries from a markdown file."""
    entries = []
    for entry in text.split(ENTRY_DELIMITER):
        entry = entry.strip()
        if entry and len(entry) > 10:
            entries.append(entry)
    return entries


def import_hermes(
    charon_state_dir: Path,
    hermes_home: Path | None = None,
    *,
    container_tag: str = "hermes",
    import_messages: bool = True,
    message_limit: int = 5000,
) -> dict[str, int]:
    """Import Hermes agent memory into Charon's semantic memory engine.

    Imports:
      - MEMORY.md entries → memories (tier="project")
      - USER.md entries → memories (tier="user", is_static=True)
      - state.db messages → memories (tier="agent", from conversations)

    Returns dict with counts of imported items.
    """
    from charon.memory.memory_engine import MemoryEngine

    hermes_dir = hermes_home or HERMES_HOME
    engine = MemoryEngine(charon_state_dir)
    stats = {"memory_entries": 0, "user_entries": 0, "messages": 0, "skipped": 0}

    # Import MEMORY.md
    memory_file = hermes_dir / "memories" / "MEMORY.md"
    if memory_file.exists():
        text = memory_file.read_text(encoding="utf-8")
        entries = _parse_section_entries(text)
        for entry in entries:
            try:
                engine.add(
                    entry,
                    category="knowledge",
                    tier="project",
                    container_tag=container_tag,
                    source_agent="hermes",
                    is_static=False,
                    check_updates=True,
                )
                stats["memory_entries"] += 1
            except Exception:
                stats["skipped"] += 1

    # Import USER.md
    user_file = hermes_dir / "memories" / "USER.md"
    if user_file.exists():
        text = user_file.read_text(encoding="utf-8")
        entries = _parse_section_entries(text)
        for entry in entries:
            try:
                engine.add(
                    entry,
                    category="preference",
                    tier="user",
                    container_tag=container_tag,
                    source_agent="hermes",
                    is_static=True,
                    check_updates=True,
                )
                stats["user_entries"] += 1
            except Exception:
                stats["skipped"] += 1

    # Import conversation messages from state.db
    if import_messages:
        state_db = hermes_dir / "state.db"
        if state_db.exists():
            try:
                db = sqlite3.connect(str(state_db))
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT role, content, session_id, timestamp FROM messages "
                    "WHERE role IN ('user', 'assistant') AND length(content) > 30 "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (message_limit,)
                ).fetchall()
                db.close()

                for row in rows:
                    content = row["content"]
                    if isinstance(content, bytes):
                        content = content.decode("utf-8", errors="ignore")
                    if not content or len(content) < 30:
                        continue
                    try:
                        engine.add(
                            content[:2000],
                            category="event",
                            tier="agent",
                            container_tag=container_tag,
                            source_agent="hermes",
                            source_conv=row["session_id"],
                            check_updates=False,
                        )
                        stats["messages"] += 1
                    except Exception:
                        stats["skipped"] += 1
            except Exception:
                pass

    engine.close()
    return stats


def hermes_available(hermes_home: Path | None = None) -> bool:
    """Check if Hermes memory files exist."""
    hermes_dir = hermes_home or HERMES_HOME
    return (hermes_dir / "memories" / "MEMORY.md").exists()
