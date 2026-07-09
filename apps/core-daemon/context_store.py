"""Lossless context store — persists every conversation message in SQLite.

Every message (user, assistant, tool_result) is written to the database
at ingest time and never deleted.  Compaction operates on a separate
context_window table that tracks what the model currently sees — a mix
of raw message references and summary references ordered by ordinal.

Tables (added to charon.db via ensure_schema):

    conversation_messages   — every message, never deleted
    conversation_summaries  — DAG of summaries (leaf + condensed)
    context_window          — ordered view the model sees per agent
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from providers import Message, ToolCall


# ── Schema ──────────────────────────────────────────────────────────

_CONTEXT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversation_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT    NOT NULL,
    seq         INTEGER NOT NULL,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL DEFAULT '',
    tool_calls  TEXT,
    tool_call_id TEXT,
    tool_name   TEXT,
    is_error    INTEGER NOT NULL DEFAULT 0,
    thinking    TEXT,
    token_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cmsg_agent     ON conversation_messages(agent_id);
CREATE INDEX IF NOT EXISTS idx_cmsg_agent_seq ON conversation_messages(agent_id, seq);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    summary_id          TEXT PRIMARY KEY,
    agent_id            TEXT    NOT NULL,
    kind                TEXT    NOT NULL,
    depth               INTEGER NOT NULL DEFAULT 0,
    content             TEXT    NOT NULL,
    token_count         INTEGER NOT NULL DEFAULT 0,
    earliest_at         TEXT,
    latest_at           TEXT,
    source_message_ids  TEXT,
    parent_summary_ids  TEXT,
    descendant_count    INTEGER NOT NULL DEFAULT 0,
    model               TEXT,
    created_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_csum_agent ON conversation_summaries(agent_id);
CREATE INDEX IF NOT EXISTS idx_csum_depth ON conversation_summaries(agent_id, depth);

CREATE TABLE IF NOT EXISTS context_window (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT    NOT NULL,
    ordinal     INTEGER NOT NULL,
    item_type   TEXT    NOT NULL,
    message_id  INTEGER,
    summary_id  TEXT,
    token_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cwin_agent ON context_window(agent_id);
CREATE INDEX IF NOT EXISTS idx_cwin_ord   ON context_window(agent_id, ordinal);
"""


# ── Helpers ─────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


def _generate_summary_id(content: str) -> str:
    h = hashlib.sha256((content + str(time.time())).encode()).hexdigest()[:16]
    return f"sum_{h}"


def _serialize_tool_calls(tool_calls: list[ToolCall]) -> str | None:
    if not tool_calls:
        return None
    return json.dumps([
        {'id': tc.id, 'name': tc.name, 'arguments': tc.arguments}
        for tc in tool_calls
    ], ensure_ascii=False)


def _deserialize_tool_calls(raw: str | None) -> list[ToolCall]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
        return [
            ToolCall(id=tc.get('id', ''), name=tc.get('name', ''),
                     arguments=tc.get('arguments', {}))
            for tc in items
        ]
    except Exception:
        return []


def _message_content_text(msg: Message) -> str:
    """Extract plain text from message content (string or block array)."""
    if isinstance(msg.content, str):
        return msg.content
    if isinstance(msg.content, list):
        parts = []
        for block in msg.content:
            if isinstance(block, dict):
                text = block.get('text', '')
                if text:
                    parts.append(text)
        return ' '.join(parts)
    return ''


def _estimate_message_tokens(msg: Message) -> int:
    """Estimate total tokens for a message including all parts."""
    total = _estimate_tokens(_message_content_text(msg))
    if msg.thinking:
        total += _estimate_tokens(msg.thinking)
    for tc in msg.tool_calls:
        total += _estimate_tokens(json.dumps(tc.arguments)) + _estimate_tokens(tc.name) + 12
    return max(1, total)


# ── Data classes ────────────────────────────────────────────────────

@dataclass
class StoredMessage:
    """A message as persisted in the database."""
    id: int
    agent_id: str
    seq: int
    role: str
    content: str
    tool_calls: list[ToolCall]
    tool_call_id: str | None
    tool_name: str | None
    is_error: bool
    thinking: str
    token_count: int
    created_at: str


@dataclass
class StoredSummary:
    """A summary node in the DAG."""
    summary_id: str
    agent_id: str
    kind: str           # 'leaf' or 'condensed'
    depth: int
    content: str
    token_count: int
    earliest_at: str | None
    latest_at: str | None
    source_message_ids: list[int]
    parent_summary_ids: list[str]
    descendant_count: int
    model: str | None
    created_at: str


@dataclass
class ContextItem:
    """A single entry in the context window."""
    id: int
    agent_id: str
    ordinal: int
    item_type: str      # 'message' or 'summary'
    message_id: int | None
    summary_id: str | None
    token_count: int


# ── ContextStore ────────────────────────────────────────────────────

class ContextStore:
    """Lossless conversation storage backed by Charon's SQLite DB.

    All public methods accept a ``db`` handle from ``store_adapter.get_db()``.
    The store never deletes raw messages — compaction only swaps references
    in the context_window table.
    """

    # ── Schema ──────────────────────────────────────────────────────

    @staticmethod
    def ensure_schema(db) -> None:
        """Create context tables if they don't exist.  Idempotent."""
        db.conn.executescript(_CONTEXT_SCHEMA_SQL)
        db.commit()

    # ── Messages ────────────────────────────────────────────────────

    @staticmethod
    def persist_message(db, agent_id: str, msg: Message) -> int:
        """Persist a message and append it to the context window.

        Returns the new message row id.
        """
        seq = ContextStore.next_seq(db, agent_id)
        content_text = _message_content_text(msg)
        token_count = _estimate_message_tokens(msg)
        now = _now_iso()

        cursor = db.execute(
            """INSERT INTO conversation_messages
               (agent_id, seq, role, content, tool_calls, tool_call_id,
                tool_name, is_error, thinking, token_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_id, seq, msg.role, content_text,
             _serialize_tool_calls(msg.tool_calls),
             msg.tool_call_id, msg.tool_name,
             int(msg.is_error), msg.thinking or '',
             token_count, now),
        )
        msg_id = cursor.lastrowid
        db.commit()

        # Append to context window
        ordinal = ContextStore.next_ordinal(db, agent_id)
        db.execute(
            """INSERT INTO context_window
               (agent_id, ordinal, item_type, message_id, summary_id, token_count)
               VALUES (?, ?, 'message', ?, NULL, ?)""",
            (agent_id, ordinal, msg_id, token_count),
        )
        db.commit()
        return msg_id

    @staticmethod
    def get_message(db, message_id: int) -> StoredMessage | None:
        """Fetch a single message by id."""
        row = db.fetchone(
            "SELECT * FROM conversation_messages WHERE id = ?", (message_id,))
        return ContextStore._row_to_message(row) if row else None

    @staticmethod
    def get_messages_by_ids(db, ids: list[int]) -> list[StoredMessage]:
        """Fetch multiple messages by id, preserving order."""
        if not ids:
            return []
        placeholders = ','.join('?' for _ in ids)
        rows = db.fetchall(
            f"SELECT * FROM conversation_messages WHERE id IN ({placeholders}) "
            f"ORDER BY seq",
            tuple(ids),
        )
        return [ContextStore._row_to_message(r) for r in rows]

    @staticmethod
    def get_messages_for_agent(db, agent_id: str, *, limit: int = 1000) -> list[StoredMessage]:
        """Fetch all messages for an agent ordered by seq."""
        rows = db.fetchall(
            "SELECT * FROM conversation_messages WHERE agent_id = ? "
            "ORDER BY seq LIMIT ?",
            (agent_id, limit),
        )
        return [ContextStore._row_to_message(r) for r in rows]

    @staticmethod
    def message_count(db, agent_id: str) -> int:
        row = db.fetchone(
            "SELECT COUNT(*) as cnt FROM conversation_messages WHERE agent_id = ?",
            (agent_id,),
        )
        return row['cnt'] if row else 0

    @staticmethod
    def next_seq(db, agent_id: str) -> int:
        row = db.fetchone(
            "SELECT COALESCE(MAX(seq), -1) + 1 as next_seq "
            "FROM conversation_messages WHERE agent_id = ?",
            (agent_id,),
        )
        return row['next_seq'] if row else 0

    # ── Summaries ───────────────────────────────────────────────────

    @staticmethod
    def insert_summary(
        db,
        *,
        agent_id: str,
        kind: str,
        depth: int,
        content: str,
        source_message_ids: list[int] | None = None,
        parent_summary_ids: list[str] | None = None,
        earliest_at: str | None = None,
        latest_at: str | None = None,
        descendant_count: int = 0,
        model: str | None = None,
    ) -> str:
        """Insert a summary and return its id."""
        summary_id = _generate_summary_id(content)
        token_count = _estimate_tokens(content)
        now = _now_iso()

        db.execute(
            """INSERT INTO conversation_summaries
               (summary_id, agent_id, kind, depth, content, token_count,
                earliest_at, latest_at, source_message_ids, parent_summary_ids,
                descendant_count, model, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (summary_id, agent_id, kind, depth, content, token_count,
             earliest_at, latest_at,
             json.dumps(source_message_ids or []),
             json.dumps(parent_summary_ids or []),
             descendant_count, model, now),
        )
        db.commit()
        return summary_id

    @staticmethod
    def get_summary(db, summary_id: str) -> StoredSummary | None:
        row = db.fetchone(
            "SELECT * FROM conversation_summaries WHERE summary_id = ?",
            (summary_id,),
        )
        return ContextStore._row_to_summary(row) if row else None

    @staticmethod
    def get_summaries_for_agent(db, agent_id: str) -> list[StoredSummary]:
        rows = db.fetchall(
            "SELECT * FROM conversation_summaries WHERE agent_id = ? "
            "ORDER BY created_at",
            (agent_id,),
        )
        return [ContextStore._row_to_summary(r) for r in rows]

    # ── Context window ──────────────────────────────────────────────

    @staticmethod
    def get_context_window(db, agent_id: str) -> list[ContextItem]:
        """Return the ordered context window for an agent."""
        rows = db.fetchall(
            "SELECT * FROM context_window WHERE agent_id = ? ORDER BY ordinal",
            (agent_id,),
        )
        return [ContextStore._row_to_context_item(r) for r in rows]

    @staticmethod
    def get_context_token_count(db, agent_id: str) -> int:
        """Total tokens currently in the context window."""
        row = db.fetchone(
            "SELECT COALESCE(SUM(token_count), 0) as total "
            "FROM context_window WHERE agent_id = ?",
            (agent_id,),
        )
        return row['total'] if row else 0

    @staticmethod
    def next_ordinal(db, agent_id: str) -> int:
        row = db.fetchone(
            "SELECT COALESCE(MAX(ordinal), -1) + 1 as next_ord "
            "FROM context_window WHERE agent_id = ?",
            (agent_id,),
        )
        return row['next_ord'] if row else 0

    @staticmethod
    def replace_range_with_summary(
        db,
        agent_id: str,
        start_ordinal: int,
        end_ordinal: int,
        summary_id: str,
        summary_token_count: int,
    ) -> None:
        """Replace a range of context items with a single summary item.

        Deletes items in [start_ordinal, end_ordinal] and inserts one
        summary item at start_ordinal.  Re-numbers remaining items so
        ordinals stay contiguous.
        """
        # Delete the range
        db.execute(
            "DELETE FROM context_window "
            "WHERE agent_id = ? AND ordinal >= ? AND ordinal <= ?",
            (agent_id, start_ordinal, end_ordinal),
        )

        # Insert the summary item at start_ordinal
        db.execute(
            """INSERT INTO context_window
               (agent_id, ordinal, item_type, message_id, summary_id, token_count)
               VALUES (?, ?, 'summary', NULL, ?, ?)""",
            (agent_id, start_ordinal, summary_id, summary_token_count),
        )

        # Re-number: shift items that were after end_ordinal down
        gap = end_ordinal - start_ordinal  # items removed minus 1 inserted
        if gap > 0:
            db.execute(
                "UPDATE context_window SET ordinal = ordinal - ? "
                "WHERE agent_id = ? AND ordinal > ?",
                (gap, agent_id, end_ordinal),
            )

        db.commit()

    @staticmethod
    def clear_context_window(db, agent_id: str) -> None:
        """Clear the context window (for reset).  Messages are NOT deleted."""
        db.execute(
            "DELETE FROM context_window WHERE agent_id = ?", (agent_id,))
        db.commit()

    # ── Bulk import ─────────────────────────────────────────────────

    @staticmethod
    def import_messages(db, agent_id: str, messages: list[Message]) -> int:
        """Import a list of messages (e.g. from JSONL migration).

        Skips messages if the agent already has messages in the DB.
        Returns the number of messages imported.
        """
        if ContextStore.message_count(db, agent_id) > 0:
            return 0

        count = 0
        for msg in messages:
            ContextStore.persist_message(db, agent_id, msg)
            count += 1
        return count

    # ── Search support ──────────────────────────────────────────────

    @staticmethod
    def search_messages(db, query: str, *, agent_id: str | None = None,
                        limit: int = 20) -> list[StoredMessage]:
        """Simple LIKE search over message content (FTS5 fallback)."""
        pattern = f"%{query}%"
        if agent_id:
            rows = db.fetchall(
                "SELECT * FROM conversation_messages "
                "WHERE agent_id = ? AND content LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (agent_id, pattern, limit),
            )
        else:
            rows = db.fetchall(
                "SELECT * FROM conversation_messages "
                "WHERE content LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (pattern, limit),
            )
        return [ContextStore._row_to_message(r) for r in rows]

    @staticmethod
    def search_summaries(db, query: str, *, agent_id: str | None = None,
                         limit: int = 20) -> list[StoredSummary]:
        """Simple LIKE search over summary content."""
        pattern = f"%{query}%"
        if agent_id:
            rows = db.fetchall(
                "SELECT * FROM conversation_summaries "
                "WHERE agent_id = ? AND content LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (agent_id, pattern, limit),
            )
        else:
            rows = db.fetchall(
                "SELECT * FROM conversation_summaries "
                "WHERE content LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (pattern, limit),
            )
        return [ContextStore._row_to_summary(r) for r in rows]

    # ── Integrity ───────────────────────────────────────────────────

    @staticmethod
    def verify_integrity(db, agent_id: str) -> list[str]:
        """Run basic integrity checks.  Returns list of issues found."""
        issues: list[str] = []
        items = ContextStore.get_context_window(db, agent_id)

        # Check ordinal contiguity
        for i, item in enumerate(items):
            if item.ordinal != i:
                issues.append(
                    f"Ordinal gap: expected {i}, got {item.ordinal}")
                break

        # Check dangling references
        for item in items:
            if item.item_type == 'message' and item.message_id:
                if not ContextStore.get_message(db, item.message_id):
                    issues.append(
                        f"Dangling message ref: id={item.message_id}")
            elif item.item_type == 'summary' and item.summary_id:
                if not ContextStore.get_summary(db, item.summary_id):
                    issues.append(
                        f"Dangling summary ref: id={item.summary_id}")

        return issues

    # ── Private helpers ─────────────────────────────────────────────

    @staticmethod
    def _row_to_message(row: dict) -> StoredMessage:
        return StoredMessage(
            id=row['id'],
            agent_id=row['agent_id'],
            seq=row['seq'],
            role=row['role'],
            content=row['content'],
            tool_calls=_deserialize_tool_calls(row.get('tool_calls')),
            tool_call_id=row.get('tool_call_id'),
            tool_name=row.get('tool_name'),
            is_error=bool(row.get('is_error', 0)),
            thinking=row.get('thinking') or '',
            token_count=row.get('token_count', 0),
            created_at=row['created_at'],
        )

    @staticmethod
    def _row_to_summary(row: dict) -> StoredSummary:
        return StoredSummary(
            summary_id=row['summary_id'],
            agent_id=row['agent_id'],
            kind=row['kind'],
            depth=row['depth'],
            content=row['content'],
            token_count=row.get('token_count', 0),
            earliest_at=row.get('earliest_at'),
            latest_at=row.get('latest_at'),
            source_message_ids=json.loads(row.get('source_message_ids') or '[]'),
            parent_summary_ids=json.loads(row.get('parent_summary_ids') or '[]'),
            descendant_count=row.get('descendant_count', 0),
            model=row.get('model'),
            created_at=row['created_at'],
        )

    @staticmethod
    def _row_to_context_item(row: dict) -> ContextItem:
        return ContextItem(
            id=row['id'],
            agent_id=row['agent_id'],
            ordinal=row['ordinal'],
            item_type=row['item_type'],
            message_id=row.get('message_id'),
            summary_id=row.get('summary_id'),
            token_count=row.get('token_count', 0),
        )
