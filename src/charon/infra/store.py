"""
Charon SQLite persistence layer.

Provides transactional, concurrent-safe storage for all Charon state:
agents, tasks (queue), events, shade contracts, boundaries, goals,
conversations, user model, and the intervention graph.

Design principles:
- WAL mode for concurrent readers + single writer without blocking.
- All writes go through transactions — no partial state.
- JSON columns for flexible nested data (task metadata, phase lists, etc.).
- Append-only event tables preserve full history.
- Every public function takes a `db` handle (returned by `open_db`).
- Thread-safe: one connection per thread with check_same_thread=False
  and IMMEDIATE transactions for writes.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


# ---------------------------------------------------------------------------
# Database lifecycle
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- -------------------------------------------------------
-- Agents
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS agents (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'persistent',
    goal            TEXT NOT NULL DEFAULT '',
    project         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'running',
    role            TEXT NOT NULL DEFAULT 'charon',
    visibility      TEXT NOT NULL DEFAULT 'user',
    parent_agent_id TEXT,
    tmux_session    TEXT,
    created_at      TEXT NOT NULL,
    last_active     TEXT NOT NULL,
    extra           TEXT NOT NULL DEFAULT '{}'
);

-- -------------------------------------------------------
-- Task queue
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    instruction     TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    task_type       TEXT NOT NULL DEFAULT '',
    owner_agent_id  TEXT,
    actor_agent_id  TEXT,
    conversation_id TEXT,
    project         TEXT,
    priority        TEXT NOT NULL DEFAULT 'normal',
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    result_summary  TEXT,
    correlation_id  TEXT,
    wait_state      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    started_at      TEXT,
    completed_at    TEXT,
    extra           TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_owner  ON tasks(owner_agent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_type   ON tasks(task_type);

-- -------------------------------------------------------
-- Events (append-only log)
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    actor_type      TEXT NOT NULL DEFAULT 'system',
    actor_id        TEXT,
    conversation_id TEXT,
    message_id      TEXT,
    parent_message_id TEXT,
    intervention_of_message_id TEXT,
    correlation_id  TEXT,
    causation_id    TEXT,
    branch_label    TEXT,
    payload         TEXT NOT NULL DEFAULT '{}',
    schema_version  TEXT NOT NULL DEFAULT '1.0',
    signature       TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_conv    ON events(conversation_id);
CREATE INDEX IF NOT EXISTS idx_events_msg     ON events(message_id);
CREATE INDEX IF NOT EXISTS idx_events_type    ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts);

-- -------------------------------------------------------
-- Shade contracts
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS shade_contracts (
    id                TEXT PRIMARY KEY,
    status            TEXT NOT NULL DEFAULT 'running',
    active_branch_id  TEXT NOT NULL DEFAULT 'main',
    parent_task_id    TEXT,
    parent_agent_id   TEXT,
    shade_agent_id    TEXT,
    conversation_id   TEXT,
    project           TEXT NOT NULL DEFAULT '',
    goal              TEXT NOT NULL DEFAULT '',
    constraints       TEXT NOT NULL DEFAULT '[]',
    expected_outputs  TEXT NOT NULL DEFAULT '[]',
    scope             TEXT NOT NULL DEFAULT '[]',
    phases            TEXT NOT NULL DEFAULT '[]',
    phase_count       INTEGER NOT NULL DEFAULT 0,
    current_phase_id  TEXT,
    branch_history    TEXT NOT NULL DEFAULT '[]',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    completed_at      TEXT,
    last_error        TEXT
);

-- -------------------------------------------------------
-- Shade phase events (append-only)
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS shade_phase_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    contract_id  TEXT NOT NULL,
    phase_id     TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_spe_contract ON shade_phase_events(contract_id);

-- -------------------------------------------------------
-- Boundaries
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS boundaries (
    id                  TEXT PRIMARY KEY,
    status              TEXT NOT NULL DEFAULT 'proposed',
    proposer_agent_id   TEXT NOT NULL,
    target_agent_id     TEXT NOT NULL,
    project             TEXT NOT NULL DEFAULT '',
    scope               TEXT NOT NULL DEFAULT '[]',
    reason              TEXT NOT NULL DEFAULT '',
    source_task_id      TEXT,
    correlation_id      TEXT,
    resolved_at         TEXT,
    resolved_by         TEXT,
    resolution_reason   TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- -------------------------------------------------------
-- Goals / projects / sessions
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS goal_projects (
    project_id   TEXT PRIMARY KEY,
    doc          TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS goal_sessions (
    session_id   TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    doc          TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS goal_context_packets (
    agent_id     TEXT PRIMARY KEY,
    packet       TEXT NOT NULL DEFAULT '{}'
);

-- -------------------------------------------------------
-- Agent runtime state (profile, working memory, inbox)
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_profiles (
    agent_id    TEXT PRIMARY KEY,
    doc         TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS agent_working_memory (
    agent_id    TEXT PRIMARY KEY,
    doc         TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS agent_inbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    ts          TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_inbox_agent ON agent_inbox(agent_id);

CREATE TABLE IF NOT EXISTS agent_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    attempt_id  TEXT NOT NULL,
    stage       TEXT NOT NULL,
    ts          TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_attempts_agent ON agent_attempts(agent_id);
CREATE INDEX IF NOT EXISTS idx_attempts_task  ON agent_attempts(task_id);

-- -------------------------------------------------------
-- User model
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_model (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL DEFAULT '{}'
);

-- -------------------------------------------------------
-- Run log (append-only, mirrors JSONL run.log)
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    event      TEXT NOT NULL,
    data       TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_runlog_ts ON run_log(ts);

-- -------------------------------------------------------
-- Onboarding
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS onboarding (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    doc   TEXT NOT NULL DEFAULT '{}'
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = 'id') -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _json_loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception as e:
        _diag('store', 'corrupt JSON column in DB row; value silently replaced by default', error=e)
        return default


class DB:
    """Thin wrapper around sqlite3 connection with convenience helpers."""

    def __init__(self, conn: sqlite3.Connection, path: Path):
        self.conn = conn
        self.path = path

    def close(self):
        self.conn.close()

    # -- low-level helpers ------------------------------------------------

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, params_seq) -> sqlite3.Cursor:
        return self.conn.executemany(sql, params_seq)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def fetchone(self, sql: str, params: tuple | dict = ()) -> dict | None:
        cur = self.conn.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row, strict=False))

    def fetchall(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        cur = self.conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def open_db(state_dir: Path, *, filename: str = 'charon.db') -> DB:
    """Open (or create) the Charon SQLite database in state_dir."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / filename
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(_SCHEMA_SQL)
    # stamp schema version
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        ('schema_version', str(_SCHEMA_VERSION)),
    )
    conn.commit()
    return DB(conn, db_path)


# ===================================================================
# AGENTS
# ===================================================================

def agent_insert(db: DB, agent: dict) -> dict:
    """Insert a new agent. Returns the agent dict."""
    now = _utc_now()
    agent.setdefault('created_at', now)
    agent.setdefault('last_active', now)
    extra = {k: v for k, v in agent.items()
             if k not in ('id', 'name', 'mode', 'goal', 'project', 'status',
                          'role', 'visibility', 'parent_agent_id',
                          'tmux_session', 'created_at', 'last_active')}
    db.execute(
        """INSERT INTO agents
           (id, name, mode, goal, project, status, role, visibility,
            parent_agent_id, tmux_session, created_at, last_active, extra)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            agent['id'], agent.get('name', ''), agent.get('mode', 'persistent'),
            agent.get('goal', ''), agent.get('project', ''),
            agent.get('status', 'running'), agent.get('role', 'charon'),
            agent.get('visibility', 'user'), agent.get('parent_agent_id'),
            agent.get('tmux_session'), agent['created_at'], agent['last_active'],
            _json_dumps(extra),
        ),
    )
    db.commit()
    return agent


def agent_get(db: DB, agent_id: str) -> dict | None:
    """Get agent by id, with extra fields merged in."""
    row = db.fetchone("SELECT * FROM agents WHERE id = ?", (agent_id,))
    if not row:
        return None
    return _hydrate_agent(row)


def agent_list(db: DB) -> list[dict]:
    """List all agents."""
    rows = db.fetchall("SELECT * FROM agents ORDER BY created_at")
    return [_hydrate_agent(r) for r in rows]


def agent_update(db: DB, agent_id: str, **fields) -> dict | None:
    """Update specific fields on an agent."""
    agent = agent_get(db, agent_id)
    if not agent:
        return None
    fields['last_active'] = _utc_now()
    known_cols = {'name', 'mode', 'goal', 'project', 'status', 'role',
                  'visibility', 'parent_agent_id', 'tmux_session',
                  'created_at', 'last_active'}
    col_updates = []
    col_values = []
    extra = _json_loads(agent.get('_raw_extra'), {})
    for k, v in fields.items():
        if k in known_cols:
            col_updates.append(f"{k} = ?")
            col_values.append(v)
        else:
            extra[k] = v
    col_updates.append("extra = ?")
    col_values.append(_json_dumps(extra))
    col_values.append(agent_id)
    db.execute(
        f"UPDATE agents SET {', '.join(col_updates)} WHERE id = ?",
        tuple(col_values),
    )
    db.commit()
    return agent_get(db, agent_id)


def agent_count(db: DB) -> int:
    row = db.fetchone("SELECT COUNT(*) as cnt FROM agents")
    return row['cnt'] if row else 0


def _hydrate_agent(row: dict) -> dict:
    extra = _json_loads(row.pop('extra', '{}'), {})
    row['_raw_extra'] = _json_dumps(extra)
    row.update(extra)
    return row


# ===================================================================
# TASK QUEUE
# ===================================================================

def task_insert(db: DB, task: dict) -> dict:
    """Insert a new task into the queue."""
    now = _utc_now()
    task.setdefault('created_at', now)
    task.setdefault('updated_at', now)
    # Separate known columns from extra JSON
    _known = {
        'id', 'title', 'instruction', 'status', 'task_type',
        'owner_agent_id', 'actor_agent_id', 'conversation_id',
        'project', 'priority', 'attempt_count', 'max_attempts',
        'result_summary', 'correlation_id', 'wait_state',
        'created_at', 'updated_at', 'started_at', 'completed_at',
    }
    extra = {k: v for k, v in task.items() if k not in _known}
    db.execute(
        """INSERT INTO tasks
           (id, title, instruction, status, task_type,
            owner_agent_id, actor_agent_id, conversation_id,
            project, priority, attempt_count, max_attempts,
            result_summary, correlation_id, wait_state,
            created_at, updated_at, started_at, completed_at, extra)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task['id'], task.get('title', ''), task.get('instruction', ''),
            task.get('status', 'pending'), task.get('task_type', ''),
            task.get('owner_agent_id'), task.get('actor_agent_id'),
            task.get('conversation_id'), task.get('project'),
            task.get('priority', 'normal'),
            int(task.get('attempt_count', 0)), int(task.get('max_attempts', 3)),
            task.get('result_summary'), task.get('correlation_id'),
            task.get('wait_state'),
            task['created_at'], task['updated_at'],
            task.get('started_at'), task.get('completed_at'),
            _json_dumps(extra),
        ),
    )
    db.commit()
    return task


def task_get(db: DB, task_id: str) -> dict | None:
    row = db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return _hydrate_task(row) if row else None


def task_list(db: DB, *, status: str | None = None,
              owner_agent_id: str | None = None,
              task_type: str | None = None,
              limit: int = 500) -> list[dict]:
    """List tasks with optional filters."""
    clauses = []
    params: list = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if owner_agent_id:
        clauses.append("owner_agent_id = ?")
        params.append(owner_agent_id)
    if task_type:
        clauses.append("task_type = ?")
        params.append(task_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.fetchall(
        f"SELECT * FROM tasks {where} ORDER BY created_at LIMIT ?",
        tuple(params + [limit]),
    )
    return [_hydrate_task(r) for r in rows]


def task_update(db: DB, task_id: str, **fields) -> dict | None:
    """Update specific fields on a task.  Extra fields go into JSON."""
    task = task_get(db, task_id)
    if not task:
        return None
    fields.setdefault('updated_at', _utc_now())
    _known = {
        'title', 'instruction', 'status', 'task_type',
        'owner_agent_id', 'actor_agent_id', 'conversation_id',
        'project', 'priority', 'attempt_count', 'max_attempts',
        'result_summary', 'correlation_id', 'wait_state',
        'created_at', 'updated_at', 'started_at', 'completed_at',
    }
    col_updates = []
    col_values = []
    extra = _json_loads(task.get('_raw_extra'), {})
    for k, v in fields.items():
        if k in _known:
            col_updates.append(f"{k} = ?")
            col_values.append(v)
        else:
            extra[k] = v
    col_updates.append("extra = ?")
    col_values.append(_json_dumps(extra))
    col_values.append(task_id)
    db.execute(
        f"UPDATE tasks SET {', '.join(col_updates)} WHERE id = ?",
        tuple(col_values),
    )
    db.commit()
    return task_get(db, task_id)


def task_delete(db: DB, task_id: str) -> bool:
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    db.commit()
    return True


def task_pending(db: DB, limit: int = 50) -> list[dict]:
    """Return pending tasks ordered by creation time."""
    rows = db.fetchall(
        "SELECT * FROM tasks WHERE status = 'pending' ORDER BY created_at LIMIT ?",
        (limit,),
    )
    return [_hydrate_task(r) for r in rows]


def task_all(db: DB) -> list[dict]:
    """Return all tasks (the full queue)."""
    rows = db.fetchall("SELECT * FROM tasks ORDER BY created_at")
    return [_hydrate_task(r) for r in rows]


def task_queue_stats(db: DB) -> dict:
    """Quick stats about the task queue."""
    row = db.fetchone("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'pending')     AS pending,
            COUNT(*) FILTER (WHERE status = 'in_progress') AS in_progress,
            COUNT(*) FILTER (WHERE status = 'completed')   AS completed,
            COUNT(*) FILTER (WHERE status = 'failed')      AS failed,
            COUNT(*)                                        AS total
        FROM tasks
    """)
    return dict(row) if row else {'pending': 0, 'in_progress': 0, 'completed': 0, 'failed': 0, 'total': 0}


def _hydrate_task(row: dict) -> dict:
    extra = _json_loads(row.pop('extra', '{}'), {})
    row['_raw_extra'] = _json_dumps(extra)
    row.update(extra)
    return row


# ===================================================================
# EVENTS (append-only intervention graph + general events)
# ===================================================================

def event_append(db: DB, event: dict) -> dict:
    """Append a single event to the events table."""
    event.setdefault('id', _new_id('evt'))
    event.setdefault('ts', _utc_now())
    event.setdefault('schema_version', '1.0')
    payload = event.get('payload', {})
    if isinstance(payload, dict):
        payload = _json_dumps(payload)
    db.execute(
        """INSERT INTO events
           (id, ts, event_type, actor_type, actor_id,
            conversation_id, message_id, parent_message_id,
            intervention_of_message_id, correlation_id, causation_id,
            branch_label, payload, schema_version, signature)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event['id'], event['ts'], event.get('event_type', ''),
            event.get('actor_type', 'system'), event.get('actor_id'),
            event.get('conversation_id'), event.get('message_id'),
            event.get('parent_message_id'), event.get('intervention_of_message_id'),
            event.get('correlation_id'), event.get('causation_id'),
            event.get('branch_label'), payload,
            event['schema_version'], event.get('signature'),
        ),
    )
    db.commit()
    return event


def event_list(db: DB, *, conversation_id: str | None = None,
               event_type: str | None = None,
               limit: int = 1000) -> list[dict]:
    clauses = []
    params: list = []
    if conversation_id:
        clauses.append("conversation_id = ?")
        params.append(conversation_id)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.fetchall(
        f"SELECT * FROM events {where} ORDER BY ts LIMIT ?",
        tuple(params + [limit]),
    )
    for r in rows:
        r['payload'] = _json_loads(r.get('payload'), {})
    return rows


def event_get_by_message(db: DB, message_id: str) -> dict | None:
    row = db.fetchone("SELECT * FROM events WHERE message_id = ?", (message_id,))
    if row:
        row['payload'] = _json_loads(row.get('payload'), {})
    return row


def events_for_conversation(db: DB, conversation_id: str) -> list[dict]:
    """All events in a conversation, ordered by timestamp."""
    return event_list(db, conversation_id=conversation_id, limit=10000)


def conversation_list(db: DB) -> list[dict]:
    """List conversations with message counts (derived from events)."""
    rows = db.fetchall("""
        SELECT conversation_id,
               COUNT(*) AS message_count,
               MAX(message_id) AS last_message_id,
               GROUP_CONCAT(DISTINCT actor_id) AS agents_csv
        FROM events
        WHERE conversation_id IS NOT NULL AND message_id IS NOT NULL
        GROUP BY conversation_id
        ORDER BY MAX(ts) DESC
    """)
    for r in rows:
        r['agents'] = [a for a in (r.pop('agents_csv', '') or '').split(',') if a]
    return rows


def reconstruct_path(db: DB, *, conversation_id: str, message_id: str) -> list[dict]:
    """Walk parent_message_id chain back to root for a conversation."""
    events = events_for_conversation(db, conversation_id)
    by_msg = {e['message_id']: e for e in events if e.get('message_id')}
    if message_id not in by_msg:
        return []
    chain: list[dict] = []
    cursor = by_msg[message_id]
    visited: set[str] = set()
    while cursor:
        mid = cursor.get('message_id')
        if not mid or mid in visited:
            break
        visited.add(mid)
        chain.append(cursor)
        parent = cursor.get('parent_message_id')
        cursor = by_msg.get(parent) if parent else None
    chain.reverse()
    return chain


# ===================================================================
# SHADE CONTRACTS
# ===================================================================

def contract_insert(db: DB, contract: dict) -> dict:
    now = _utc_now()
    contract.setdefault('created_at', now)
    contract.setdefault('updated_at', now)
    db.execute(
        """INSERT INTO shade_contracts
           (id, status, active_branch_id, parent_task_id, parent_agent_id,
            shade_agent_id, conversation_id, project, goal,
            constraints, expected_outputs, scope,
            phases, phase_count, current_phase_id, branch_history,
            created_at, updated_at, completed_at, last_error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            contract['id'], contract.get('status', 'running'),
            contract.get('active_branch_id', 'main'),
            contract.get('parent_task_id'), contract.get('parent_agent_id'),
            contract.get('shade_agent_id'), contract.get('conversation_id'),
            contract.get('project', ''), contract.get('goal', ''),
            _json_dumps(contract.get('constraints', [])),
            _json_dumps(contract.get('expected_outputs', [])),
            _json_dumps(contract.get('scope', [])),
            _json_dumps(contract.get('phases', [])),
            int(contract.get('phase_count', 0)),
            contract.get('current_phase_id'),
            _json_dumps(contract.get('branch_history', [])),
            contract['created_at'], contract['updated_at'],
            contract.get('completed_at'), contract.get('last_error'),
        ),
    )
    db.commit()
    return contract


def contract_get(db: DB, contract_id: str) -> dict | None:
    row = db.fetchone("SELECT * FROM shade_contracts WHERE id = ?", (contract_id,))
    return _hydrate_contract(row) if row else None


def contract_list(db: DB, *, status: str | None = None) -> list[dict]:
    if status:
        rows = db.fetchall(
            "SELECT * FROM shade_contracts WHERE status = ? ORDER BY created_at",
            (status,),
        )
    else:
        rows = db.fetchall("SELECT * FROM shade_contracts ORDER BY created_at")
    return [_hydrate_contract(r) for r in rows]


def contract_update(db: DB, contract: dict) -> dict:
    """Full update of a contract (replace all fields)."""
    contract['updated_at'] = _utc_now()
    db.execute(
        """UPDATE shade_contracts SET
            status = ?, active_branch_id = ?, parent_task_id = ?,
            parent_agent_id = ?, shade_agent_id = ?, conversation_id = ?,
            project = ?, goal = ?, constraints = ?, expected_outputs = ?,
            scope = ?, phases = ?, phase_count = ?, current_phase_id = ?,
            branch_history = ?, updated_at = ?, completed_at = ?, last_error = ?
           WHERE id = ?""",
        (
            contract.get('status', 'running'),
            contract.get('active_branch_id', 'main'),
            contract.get('parent_task_id'), contract.get('parent_agent_id'),
            contract.get('shade_agent_id'), contract.get('conversation_id'),
            contract.get('project', ''), contract.get('goal', ''),
            _json_dumps(contract.get('constraints', [])),
            _json_dumps(contract.get('expected_outputs', [])),
            _json_dumps(contract.get('scope', [])),
            _json_dumps(contract.get('phases', [])),
            int(contract.get('phase_count', 0)),
            contract.get('current_phase_id'),
            _json_dumps(contract.get('branch_history', [])),
            contract['updated_at'], contract.get('completed_at'),
            contract.get('last_error'),
            contract['id'],
        ),
    )
    db.commit()
    return contract


def _hydrate_contract(row: dict) -> dict:
    for key in ('constraints', 'expected_outputs', 'scope', 'phases', 'branch_history'):
        row[key] = _json_loads(row.get(key), [])
    return row


# ===================================================================
# SHADE PHASE EVENTS
# ===================================================================

def shade_event_append(db: DB, *, contract_id: str, phase_id: str,
                       event_type: str, payload: dict | None = None) -> None:
    db.execute(
        """INSERT INTO shade_phase_events (ts, contract_id, phase_id, event_type, payload)
           VALUES (?, ?, ?, ?, ?)""",
        (_utc_now(), contract_id, phase_id, event_type, _json_dumps(payload or {})),
    )
    db.commit()


def shade_event_list(db: DB, contract_id: str | None = None) -> list[dict]:
    if contract_id:
        rows = db.fetchall(
            "SELECT * FROM shade_phase_events WHERE contract_id = ? ORDER BY id",
            (contract_id,),
        )
    else:
        rows = db.fetchall("SELECT * FROM shade_phase_events ORDER BY id")
    for r in rows:
        r['payload'] = _json_loads(r.get('payload'), {})
    return rows


# ===================================================================
# BOUNDARIES
# ===================================================================

def boundary_insert(db: DB, boundary: dict) -> dict:
    now = _utc_now()
    boundary.setdefault('created_at', now)
    boundary.setdefault('updated_at', now)
    db.execute(
        """INSERT INTO boundaries
           (id, status, proposer_agent_id, target_agent_id, project,
            scope, reason, source_task_id, correlation_id,
            resolved_at, resolved_by, resolution_reason,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            boundary['id'], boundary.get('status', 'proposed'),
            boundary['proposer_agent_id'], boundary['target_agent_id'],
            boundary.get('project', ''),
            _json_dumps(boundary.get('scope', [])),
            boundary.get('reason', ''),
            boundary.get('source_task_id'), boundary.get('correlation_id'),
            boundary.get('resolved_at'), boundary.get('resolved_by'),
            boundary.get('resolution_reason', ''),
            boundary['created_at'], boundary['updated_at'],
        ),
    )
    db.commit()
    return boundary


def boundary_get(db: DB, boundary_id: str) -> dict | None:
    row = db.fetchone("SELECT * FROM boundaries WHERE id = ?", (boundary_id,))
    if row:
        row['scope'] = _json_loads(row.get('scope'), [])
    return row


def boundary_list(db: DB) -> list[dict]:
    rows = db.fetchall("SELECT * FROM boundaries ORDER BY created_at")
    for r in rows:
        r['scope'] = _json_loads(r.get('scope'), [])
    return rows


def boundary_update(db: DB, boundary_id: str, **fields) -> dict | None:
    boundary = boundary_get(db, boundary_id)
    if not boundary:
        return None
    fields['updated_at'] = _utc_now()
    sets = []
    vals = []
    for k, v in fields.items():
        if k == 'scope':
            v = _json_dumps(v)
        sets.append(f"{k} = ?")
        vals.append(v)
    vals.append(boundary_id)
    db.execute(f"UPDATE boundaries SET {', '.join(sets)} WHERE id = ?", tuple(vals))
    db.commit()
    return boundary_get(db, boundary_id)


def boundary_pending_for_agent(db: DB, agent_id: str) -> list[dict]:
    rows = db.fetchall(
        "SELECT * FROM boundaries WHERE target_agent_id = ? AND status = 'proposed' ORDER BY created_at",
        (agent_id,),
    )
    for r in rows:
        r['scope'] = _json_loads(r.get('scope'), [])
    return rows


# ===================================================================
# AGENT RUNTIME STATE
# ===================================================================

def agent_profile_upsert(db: DB, agent_id: str, doc: dict) -> None:
    db.execute(
        "INSERT OR REPLACE INTO agent_profiles (agent_id, doc) VALUES (?, ?)",
        (agent_id, _json_dumps(doc)),
    )
    db.commit()


def agent_profile_get(db: DB, agent_id: str) -> dict | None:
    row = db.fetchone("SELECT doc FROM agent_profiles WHERE agent_id = ?", (agent_id,))
    return _json_loads(row['doc']) if row else None


def agent_memory_upsert(db: DB, agent_id: str, doc: dict) -> None:
    db.execute(
        "INSERT OR REPLACE INTO agent_working_memory (agent_id, doc) VALUES (?, ?)",
        (agent_id, _json_dumps(doc)),
    )
    db.commit()


def agent_memory_get(db: DB, agent_id: str) -> dict | None:
    row = db.fetchone("SELECT doc FROM agent_working_memory WHERE agent_id = ?", (agent_id,))
    return _json_loads(row['doc']) if row else None


def agent_inbox_append(db: DB, agent_id: str, event_type: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO agent_inbox (agent_id, ts, event_type, payload) VALUES (?, ?, ?, ?)",
        (agent_id, _utc_now(), event_type, _json_dumps(payload)),
    )
    db.commit()


def agent_inbox_list(db: DB, agent_id: str, limit: int = 100) -> list[dict]:
    rows = db.fetchall(
        "SELECT * FROM agent_inbox WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
        (agent_id, limit),
    )
    for r in rows:
        r['payload'] = _json_loads(r.get('payload'), {})
    return rows


def agent_attempt_append(db: DB, agent_id: str, task_id: str,
                         attempt_id: str, stage: str,
                         payload: dict | None = None) -> None:
    db.execute(
        """INSERT INTO agent_attempts (agent_id, task_id, attempt_id, stage, ts, payload)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (agent_id, task_id, attempt_id, stage, _utc_now(), _json_dumps(payload or {})),
    )
    db.commit()


# ===================================================================
# GOALS
# ===================================================================

def goal_project_upsert(db: DB, project_id: str, doc: dict) -> None:
    db.execute(
        "INSERT OR REPLACE INTO goal_projects (project_id, doc) VALUES (?, ?)",
        (project_id, _json_dumps(doc)),
    )
    db.commit()


def goal_project_get(db: DB, project_id: str) -> dict | None:
    row = db.fetchone("SELECT doc FROM goal_projects WHERE project_id = ?", (project_id,))
    return _json_loads(row['doc']) if row else None


def goal_session_upsert(db: DB, session_id: str, project_id: str, doc: dict) -> None:
    db.execute(
        "INSERT OR REPLACE INTO goal_sessions (session_id, project_id, doc) VALUES (?, ?, ?)",
        (session_id, project_id, _json_dumps(doc)),
    )
    db.commit()


def goal_session_get(db: DB, session_id: str) -> dict | None:
    row = db.fetchone("SELECT doc FROM goal_sessions WHERE session_id = ?", (session_id,))
    return _json_loads(row['doc']) if row else None


def goal_context_packet_upsert(db: DB, agent_id: str, packet: dict) -> None:
    db.execute(
        "INSERT OR REPLACE INTO goal_context_packets (agent_id, packet) VALUES (?, ?)",
        (agent_id, _json_dumps(packet)),
    )
    db.commit()


def goal_context_packet_get(db: DB, agent_id: str) -> dict | None:
    row = db.fetchone("SELECT packet FROM goal_context_packets WHERE agent_id = ?", (agent_id,))
    return _json_loads(row['packet']) if row else None


# ===================================================================
# USER MODEL
# ===================================================================

def user_model_get(db: DB) -> dict:
    """Return the full user model as a single dict."""
    rows = db.fetchall("SELECT key, value FROM user_model")
    model: dict = {}
    for r in rows:
        model[r['key']] = _json_loads(r['value'])
    return model


def user_model_set(db: DB, key: str, value: Any) -> None:
    db.execute(
        "INSERT OR REPLACE INTO user_model (key, value) VALUES (?, ?)",
        (key, _json_dumps(value)),
    )
    db.commit()


# ===================================================================
# ONBOARDING
# ===================================================================

def onboarding_get(db: DB) -> dict:
    row = db.fetchone("SELECT doc FROM onboarding WHERE id = 1")
    if not row:
        return {'complete': False, 'step': 'provider-mode'}
    return _json_loads(row['doc'], {'complete': False, 'step': 'provider-mode'})


def onboarding_set(db: DB, doc: dict) -> None:
    db.execute(
        "INSERT OR REPLACE INTO onboarding (id, doc) VALUES (1, ?)",
        (_json_dumps(doc),),
    )
    db.commit()


# ===================================================================
# RUN LOG
# ===================================================================

def run_log_append(db: DB, event: str, **data) -> None:
    db.execute(
        "INSERT INTO run_log (ts, event, data) VALUES (?, ?, ?)",
        (_utc_now(), event, _json_dumps(data)),
    )
    db.commit()


def run_log_tail(db: DB, count: int = 20) -> list[dict]:
    rows = db.fetchall(
        "SELECT * FROM run_log ORDER BY id DESC LIMIT ?",
        (count,),
    )
    for r in rows:
        r['data'] = _json_loads(r.get('data'), {})
    rows.reverse()
    return rows


# ===================================================================
# MIGRATION HELPER: import from JSON files
# ===================================================================

def migrate_from_json(db: DB, state_dir: Path) -> dict:
    """
    One-time import of existing JSON/JSONL state files into SQLite.
    Returns a summary of what was migrated.
    """
    state_dir = Path(state_dir)
    summary: dict[str, int] = {}

    # -- agents.json
    agents_file = state_dir / 'agents.json'
    if agents_file.exists():
        try:
            agents = json.loads(agents_file.read_text())
            for a in (agents if isinstance(agents, list) else []):
                if not agent_get(db, a.get('id', '')):
                    agent_insert(db, a)
            summary['agents'] = len(agents)
        except Exception as e:
            _diag('store', 'agents.json migration to SQLite failed; legacy agents not imported', error=e)

    # -- queue.json
    queue_file = state_dir / 'queue.json'
    if queue_file.exists():
        try:
            tasks = json.loads(queue_file.read_text())
            for t in (tasks if isinstance(tasks, list) else []):
                if not task_get(db, t.get('id', '')):
                    task_insert(db, t)
            summary['tasks'] = len(tasks)
        except Exception as e:
            _diag('store', 'queue.json migration to SQLite failed; legacy tasks not imported', error=e)

    # -- interventions.jsonl (events / intervention graph)
    interventions_file = state_dir / 'interventions.jsonl'
    if interventions_file.exists():
        count = 0
        for line in interventions_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
                if evt.get('id') and not event_get_by_message(db, evt.get('message_id', '')):
                    event_append(db, evt)
                    count += 1
            except Exception:
                continue
        summary['events'] = count

    # -- shade_contracts.json
    contracts_file = state_dir / 'shade_contracts.json'
    if contracts_file.exists():
        try:
            contracts = json.loads(contracts_file.read_text())
            for c in (contracts if isinstance(contracts, list) else []):
                if not contract_get(db, c.get('id', '')):
                    contract_insert(db, c)
            summary['contracts'] = len(contracts)
        except Exception as e:
            _diag('store', 'shade_contracts.json migration to SQLite failed; legacy contracts not imported', error=e)

    # -- shade_phase_events.jsonl
    spe_file = state_dir / 'shade_phase_events.jsonl'
    if spe_file.exists():
        count = 0
        for line in spe_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                shade_event_append(
                    db,
                    contract_id=rec.get('contract_id', ''),
                    phase_id=rec.get('phase_id', ''),
                    event_type=rec.get('event_type', ''),
                    payload=rec.get('payload'),
                )
                count += 1
            except Exception:
                continue
        summary['shade_phase_events'] = count

    # -- boundaries.json
    boundaries_file = state_dir / 'boundaries.json'
    if boundaries_file.exists():
        try:
            boundaries = json.loads(boundaries_file.read_text())
            for b in (boundaries if isinstance(boundaries, list) else []):
                if not boundary_get(db, b.get('id', '')):
                    boundary_insert(db, b)
            summary['boundaries'] = len(boundaries)
        except Exception as e:
            _diag('store', 'boundaries.json migration to SQLite failed; legacy boundaries not imported', error=e)

    # -- onboarding.json
    onboarding_file = state_dir / 'onboarding.json'
    if onboarding_file.exists():
        try:
            doc = json.loads(onboarding_file.read_text())
            if isinstance(doc, dict):
                onboarding_set(db, doc)
                summary['onboarding'] = 1
        except Exception as e:
            _diag('store', 'onboarding.json migration to SQLite failed; legacy onboarding state not imported', error=e)

    # -- user_model.json
    um_file = state_dir / 'user_model.json'
    if um_file.exists():
        try:
            model = json.loads(um_file.read_text())
            if isinstance(model, dict):
                for k, v in model.items():
                    if k != 'updated_at':
                        user_model_set(db, k, v)
                summary['user_model_keys'] = len(model)
        except Exception as e:
            _diag('store', 'user_model.json migration to SQLite failed; legacy user model not imported', error=e)

    return summary
