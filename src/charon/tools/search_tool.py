"""Conversation search — FTS5 full-text search over conversation history.

Indexes all messages from the conversation store into an FTS5 virtual
table in SQLite. The Search tool lets agents recall past conversations.
"""
from __future__ import annotations

import json
from pathlib import Path

from charon.tools import ToolContext, ToolResult

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


# ── FTS5 index management ──────────────────────────────────────────

def _ensure_fts_table(state_dir: Path) -> None:
    """Create the FTS5 table if it doesn't exist."""
    try:
        from charon.infra.store_adapter import get_db
        db = get_db(state_dir)
        db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts USING fts5(
                agent_id,
                role,
                content,
                timestamp,
                tokenize='porter unicode61'
            )
        """)
        db.commit()
    except Exception as e:
        _diag('search_tool', 'FTS5 table creation failed; conversation search index unavailable', error=e)


def rebuild_index(state_dir: Path) -> int:
    """Rebuild the FTS5 index from conversation JSONL files.

    Returns the number of messages indexed.
    """
    _ensure_fts_table(state_dir)

    try:
        from charon.infra.store_adapter import get_db
        db = get_db(state_dir)

        # Clear existing index
        db.execute("DELETE FROM conversation_fts")

        count = 0
        conv_dir = state_dir / 'conversations'
        if not conv_dir.is_dir():
            db.commit()
            return 0

        for jsonl_file in conv_dir.glob('*.jsonl'):
            agent_id = jsonl_file.stem
            for line in jsonl_file.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    role = msg.get('role', '')
                    content = msg.get('content', '')
                    timestamp = str(msg.get('timestamp', ''))

                    if not content or role == 'tool_result':
                        continue  # skip empty and tool results (noisy)

                    # Flatten content if it's a list
                    if isinstance(content, list):
                        parts = []
                        for block in content:
                            if isinstance(block, dict) and block.get('text'):
                                parts.append(block['text'])
                        content = ' '.join(parts)

                    if not isinstance(content, str) or len(content) < 5:
                        continue

                    db.execute(
                        "INSERT INTO conversation_fts (agent_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                        (agent_id, role, content[:10000], timestamp),  # cap content at 10k chars
                    )
                    count += 1
                except Exception:
                    continue

        db.commit()
        return count
    except Exception as e:
        _diag('search_tool', 'FTS index rebuild failed; reporting 0 messages indexed', error=e)
        return 0


def search_conversations(
    state_dir: Path,
    query: str,
    *,
    agent_id: str | None = None,
    role: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search conversation history using FTS5.

    Returns list of matching messages with snippet and metadata.
    """
    _ensure_fts_table(state_dir)

    # Rebuild index if empty (first search)
    try:
        from charon.infra.store_adapter import get_db
        db = get_db(state_dir)
        row = db.fetchone("SELECT COUNT(*) as cnt FROM conversation_fts")
        if row and row['cnt'] == 0:
            rebuild_index(state_dir)
    except Exception as e:
        _diag('search_tool', 'FTS empty-index check failed; search may run against a missing/stale index', error=e)

    try:
        from charon.infra.store_adapter import get_db
        db = get_db(state_dir)

        # Build FTS5 query — escape special chars
        fts_query = query.replace('"', '').replace("'", '').strip()
        if not fts_query:
            return []

        # Use MATCH with terms
        terms = fts_query.split()
        if len(terms) > 1:
            fts_match = ' '.join(f'"{t}"' for t in terms[:10])
        else:
            fts_match = f'"{fts_query}"'

        sql = """
            SELECT agent_id, role, 
                   snippet(conversation_fts, 2, '>>>', '<<<', '...', 40) as snippet,
                   content, timestamp,
                   rank
            FROM conversation_fts
            WHERE conversation_fts MATCH ?
        """
        params: list = [fts_match]

        if agent_id:
            sql += " AND agent_id = ?"
            params.append(agent_id)
        if role:
            sql += " AND role = ?"
            params.append(role)

        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        rows = db.fetchall(sql, tuple(params))
        return rows

    except Exception as e:
        _diag('search_tool', 'conversation FTS search failed; returning no results', error=e)
        return []


# ── Search tool ─────────────────────────────────────────────────────

SEARCH_TOOL_DEF = {
    'name': 'Search',
    'description': (
        'Search past conversations and task history. '
        'Find what was discussed, what files were changed, what decisions were made. '
        'Use this when the user references something from a past session or you need context.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'Search query — keywords or phrases.',
            },
            'agent_id': {
                'type': 'string',
                'description': 'Limit search to a specific agent (optional).',
            },
            'limit': {
                'type': 'number',
                'description': 'Max results (default: 10).',
            },
        },
        'required': ['query'],
    },
}


def execute_search(params: dict, ctx: ToolContext) -> ToolResult:
    """Execute a conversation search."""
    query = str(params.get('query', '')).strip()
    if not query:
        return ToolResult(content='Error: query is required.', is_error=True)

    if not ctx.state_dir:
        return ToolResult(content='Error: state_dir not available.', is_error=True)

    agent_filter = params.get('agent_id') or None
    limit = int(params.get('limit') or 10)

    results = search_conversations(
        ctx.state_dir,
        query,
        agent_id=agent_filter,
        limit=limit,
    )

    if not results:
        return ToolResult(content=f'No results found for: {query}')

    lines = [f'Found {len(results)} result(s) for "{query}":']
    for r in results:
        agent = r.get('agent_id', '?')
        role = r.get('role', '?')
        snippet = r.get('snippet', r.get('content', '')[:150])
        lines.append(f'\n[{agent}] ({role}):')
        lines.append(f'  {snippet}')

    return ToolResult(content='\n'.join(lines))
