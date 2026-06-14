"""Semantic memory engine — local vector + FTS5 hybrid search.

SQLite + sqlite-vec for embeddings, FTS5 for keyword fallback,
reciprocal rank fusion for merging. No cloud, no API keys.

Usage:
    engine = MemoryEngine(state_dir)
    engine.add("User prefers TypeScript with strict mode", category="preference")
    results = engine.recall("programming language preferences")
"""
from __future__ import annotations

import json
import re
import sqlite3
import struct
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from embedding_client import embed_texts as _embed_texts_via_client, get_embedding_dim as _get_embedding_dim_via_client

try:
    from diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


# ── Constants ───────────────────────────────────────────────────────

# Embedding model config — override via CHARON_EMBED_MODEL env var.
# Supported: "BAAI/bge-base-en-v1.5" (768d, recommended),
#            "all-MiniLM-L6-v2" (384d, lightweight fallback),
#            "BAAI/bge-large-en-v1.5" (1024d, highest quality).
import os as _os
EMBEDDING_MODEL = _os.environ.get("CHARON_EMBED_MODEL", "BAAI/bge-base-en-v1.5")
# Dimension is detected at load time from the model itself.
EMBEDDING_DIM: int | None = None  # set lazily by _get_model()
DEFAULT_RECALL_LIMIT = 20
RRF_K = 60  # reciprocal rank fusion constant

# Stopwords dropped from FTS queries so content terms drive the match (used with
# OR/should-match semantics + BM25 ranking).
_FTS_STOPWORDS = {
    'a', 'an', 'the', 'and', 'or', 'but', 'if', 'of', 'to', 'in', 'on', 'at', 'by',
    'for', 'with', 'about', 'as', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'do', 'does', 'did', 'have', 'has', 'had', 'i', 'me', 'my', 'we', 'you', 'your',
    'it', 'its', 'this', 'that', 'these', 'those', 'what', 'which', 'who', 'whom',
    'when', 'where', 'why', 'how', 'how_many', 'many', 'much', 'so', 'than', 'then',
    'there', 'here', 'from', 'into', 'out', 'up', 'down', 'over', 'under', 'again',
    'can', 'will', 'would', 'should', 'could', 'may', 'might', 'must', 'not', 'no',
    'yes', 'all', 'any', 'some', 'me', 'mine', 'our', 'us', 'they', 'them', 'their',
}
SIMILARITY_THRESHOLD = 0.35  # minimum cosine similarity to include
DEDUP_THRESHOLD = 0.95  # cosine similarity for dedup (exact/near-exact only)
VERSION_MATCH_THRESHOLD = 0.80  # similarity to detect knowledge updates


# ── Data classes ────────────────────────────────────────────────────

@dataclass
class Memory:
    id: str
    content: str
    category: str = "general"
    tier: str = "user"  # user / project / agent
    container_tag: str = "default"
    is_static: bool = False
    is_latest: bool = True
    is_forgotten: bool = False
    version: int = 1
    parent_id: str | None = None
    source_agent: str | None = None
    source_conv: str | None = None
    source_turn: int | None = None
    event_date: str | None = None
    forget_after: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class RecallResult:
    memories: list[ScoredMemory] = field(default_factory=list)
    profile_static: list[str] = field(default_factory=list)
    profile_dynamic: list[str] = field(default_factory=list)
    confidence: float = 0.0
    timing_ms: float = 0.0


@dataclass
class ScoredMemory:
    memory: Memory
    score: float = 0.0
    source: str = ""  # "vec", "fts", "hybrid"
    version_chain: list[Memory] = field(default_factory=list)


# ── Embedding backend (shared worker by default) ───────────────────


def get_embedding_dim(state_dir: Path) -> int:
    """Return embedding dimension, using the shared embedding worker by default."""
    global EMBEDDING_DIM
    if EMBEDDING_DIM is None:
        EMBEDDING_DIM = _get_embedding_dim_via_client(state_dir)
    return int(EMBEDDING_DIM)


def embed(texts: list[str], state_dir: Path) -> list[list[float]]:
    """Embed a batch of texts through the shared worker."""
    return _embed_texts_via_client(state_dir, texts)


def embed_one(text: str, state_dir: Path) -> list[float]:
    """Embed a single text."""
    return embed([text], state_dir)[0]


def _serialize_vec(vec: list[float]) -> bytes:
    """Serialize float vector to bytes for sqlite-vec."""
    return struct.pack(f"{len(vec)}f", *vec)


def _deserialize_vec(data: bytes) -> list[float]:
    """Deserialize bytes back to float vector."""
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_rowid_counter = 0

def _uuid() -> str:
    return uuid.uuid4().hex[:16]

def _next_rowid() -> int:
    """Generate a unique integer rowid for vec0 (which requires int PKs)."""
    global _rowid_counter
    _rowid_counter += 1
    return int(time.monotonic() * 1_000_000) + _rowid_counter


# ── Engine ──────────────────────────────────────────────────────────

class MemoryEngine:
    """Local semantic memory engine backed by SQLite + sqlite-vec."""

    def __init__(self, state_dir: Path, db: Any | None = None):
        self.state_dir = state_dir
        self._db = db
        self._ensure_schema()

    def _get_db(self) -> sqlite3.Connection:
        """Get or create the SQLite connection with sqlite-vec loaded."""
        if self._db is not None:
            return self._db
        import sqlite_vec
        db_path = self.state_dir / "memory.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + busy_timeout mirror libs/store.py: the
        # engine is constructed ad-hoc across threads, and concurrent writers
        # to memory.db must wait rather than raise "database is locked".
        db = sqlite3.connect(str(db_path), check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=NORMAL")
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        self._db = db
        return db

    def _ensure_schema(self) -> None:
        """Create tables if they don't exist."""
        db = self._get_db()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id            TEXT PRIMARY KEY,
                content       TEXT NOT NULL,
                category      TEXT NOT NULL DEFAULT 'general',
                tier          TEXT NOT NULL DEFAULT 'user',
                container_tag TEXT NOT NULL DEFAULT 'default',
                is_static     INTEGER NOT NULL DEFAULT 0,
                is_latest     INTEGER NOT NULL DEFAULT 1,
                is_forgotten  INTEGER NOT NULL DEFAULT 0,
                version       INTEGER NOT NULL DEFAULT 1,
                parent_id     TEXT,
                source_agent  TEXT,
                source_conv   TEXT,
                source_turn   INTEGER,
                event_date    TEXT,
                forget_after  TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                FOREIGN KEY (parent_id) REFERENCES memories(id)
            );

            CREATE TABLE IF NOT EXISTS memory_edges (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id   TEXT NOT NULL,
                target_id   TEXT NOT NULL,
                edge_type   TEXT NOT NULL,
                confidence  REAL NOT NULL DEFAULT 1.0,
                created_at  TEXT NOT NULL,
                FOREIGN KEY (source_id) REFERENCES memories(id),
                FOREIGN KEY (target_id) REFERENCES memories(id)
            );

            CREATE INDEX IF NOT EXISTS idx_mem_container ON memories(container_tag);
            CREATE INDEX IF NOT EXISTS idx_mem_tier ON memories(tier);
            CREATE INDEX IF NOT EXISTS idx_mem_latest ON memories(is_latest);
            CREATE INDEX IF NOT EXISTS idx_mem_parent ON memories(parent_id);
            CREATE INDEX IF NOT EXISTS idx_mem_event_date ON memories(event_date);
            CREATE INDEX IF NOT EXISTS idx_mem_forgotten ON memories(is_forgotten);
            CREATE INDEX IF NOT EXISTS idx_edges_source ON memory_edges(source_id);
            CREATE INDEX IF NOT EXISTS idx_edges_target ON memory_edges(target_id);
            CREATE TABLE IF NOT EXISTS memory_vec_map (
                rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id   TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS idx_vecmap_memid ON memory_vec_map(memory_id);

            CREATE INDEX IF NOT EXISTS idx_edges_type ON memory_edges(edge_type);
        """)

        # vec0 virtual table — must be created separately (not in executescript)
        # Dimension is detected from the model at load time.
        dim = get_embedding_dim(self.state_dir)
        try:
            db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(embedding float[{dim}])")
        except Exception as e:
            # "already exists" is fine, but a genuine failure here (e.g.
            # sqlite-vec not loaded) silently degrades recall to FTS-only —
            # surface it rather than swallow it blind.
            if 'already exists' not in str(e).lower():
                _diag('memory_engine', 'vec0 virtual table unavailable; recall degrades to FTS-only',
                      state_dir=self.state_dir, error=e, dim=dim)

        # FTS5 for keyword search
        try:
            db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    memory_id,
                    content,
                    category,
                    container_tag,
                    tokenize='porter unicode61'
                )
            """)
        except Exception as e:
            if 'already exists' not in str(e).lower():
                _diag('memory_engine', 'FTS5 table unavailable; keyword search disabled',
                      state_dir=self.state_dir, error=e)

        db.commit()

    # ── Add memories ────────────────────────────────────────────────

    def add(
        self,
        content: str,
        *,
        category: str = "general",
        tier: str = "user",
        container_tag: str = "default",
        is_static: bool = False,
        event_date: str | None = None,
        forget_after: str | None = None,
        source_agent: str | None = None,
        source_conv: str | None = None,
        source_turn: int | None = None,
        check_updates: bool = True,
    ) -> Memory:
        """Add a memory. Checks for duplicates and knowledge updates."""
        content = content.strip()
        if not content:
            raise ValueError("Memory content cannot be empty")

        db = self._get_db()
        now = _now()
        mem_id = _uuid()
        vec = embed_one(content, self.state_dir)

        # Check for near-duplicate
        existing = self._find_similar(vec, container_tag=container_tag, threshold=DEDUP_THRESHOLD, limit=1)
        if existing:
            # Exact or near-duplicate — skip
            return self._row_to_memory(
                db.execute("SELECT * FROM memories WHERE id = ?", (existing[0][0],)).fetchone()
            )

        # Check for knowledge update (same topic, different content)
        parent_id = None
        version = 1
        if check_updates:
            updates = self._find_similar(
                vec, container_tag=container_tag,
                threshold=VERSION_MATCH_THRESHOLD, limit=5
            )
            for row_id, dist in updates:
                row = db.execute(
                    "SELECT * FROM memories WHERE id = ? AND is_latest = 1 AND is_forgotten = 0",
                    (row_id,)
                ).fetchone()
                if row and row["content"] != content:
                    # This looks like an update — mark old as not-latest
                    parent_id = row["id"]
                    version = (row["version"] or 1) + 1
                    db.execute(
                        "UPDATE memories SET is_latest = 0, updated_at = ? WHERE id = ?",
                        (now, parent_id)
                    )
                    # Add edge
                    db.execute(
                        "INSERT INTO memory_edges (source_id, target_id, edge_type, confidence, created_at) "
                        "VALUES (?, ?, 'updates', ?, ?)",
                        (mem_id, parent_id, 1.0 - (dist * dist / 2.0), now)
                    )
                    break

        # Insert memory
        mem = Memory(
            id=mem_id, content=content, category=category, tier=tier,
            container_tag=container_tag, is_static=is_static, is_latest=True,
            is_forgotten=False, version=version, parent_id=parent_id,
            source_agent=source_agent, source_conv=source_conv,
            source_turn=source_turn, event_date=event_date,
            forget_after=forget_after, created_at=now, updated_at=now,
        )

        db.execute(
            "INSERT INTO memories (id, content, category, tier, container_tag, "
            "is_static, is_latest, is_forgotten, version, parent_id, "
            "source_agent, source_conv, source_turn, event_date, forget_after, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mem.id, mem.content, mem.category, mem.tier, mem.container_tag,
             int(mem.is_static), int(mem.is_latest), int(mem.is_forgotten),
             mem.version, mem.parent_id, mem.source_agent, mem.source_conv,
             mem.source_turn, mem.event_date, mem.forget_after,
             mem.created_at, mem.updated_at)
        )

        # Insert vector (vec0 requires integer rowid, so we use a mapping table)
        cursor = db.execute(
            "INSERT INTO memory_vec_map (memory_id) VALUES (?)", (mem.id,)
        )
        vec_rowid = cursor.lastrowid
        db.execute(
            "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
            (vec_rowid, _serialize_vec(vec))
        )

        # Insert into FTS
        db.execute(
            "INSERT INTO memory_fts (memory_id, content, category, container_tag) "
            "VALUES (?, ?, ?, ?)",
            (mem.id, content, category, container_tag)
        )

        db.commit()
        return mem

    def add_batch(self, facts: list[dict], **defaults) -> list[Memory]:
        """Add multiple memories efficiently."""
        results = []
        for fact in facts:
            merged = {**defaults, **fact}
            content = merged.pop("content", "")
            if content:
                try:
                    mem = self.add(content, **merged)
                    results.append(mem)
                except Exception:
                    pass
        return results

    # ── Forget ──────────────────────────────────────────────────────

    def forget(self, memory_id: str) -> bool:
        """Soft-delete a memory."""
        db = self._get_db()
        db.execute(
            "UPDATE memories SET is_forgotten = 1, updated_at = ? WHERE id = ?",
            (_now(), memory_id)
        )
        db.commit()
        return db.total_changes > 0

    def expire_memories(self) -> int:
        """Expire memories past their forget_after date."""
        db = self._get_db()
        now = _now()
        db.execute(
            "UPDATE memories SET is_forgotten = 1, updated_at = ? "
            "WHERE forget_after IS NOT NULL AND forget_after < ? AND is_forgotten = 0",
            (now, now)
        )
        count = db.total_changes
        db.commit()
        return count

    # ── Recall (hybrid search) ──────────────────────────────────────

    def recall(
        self,
        query: str,
        *,
        container_tag: str | None = None,
        tier: str | None = None,
        limit: int = DEFAULT_RECALL_LIMIT,
        include_profile: bool = False,
        include_version_chains: bool = True,
        temporal_range: tuple[str, str] | None = None,
    ) -> RecallResult:
        """Hybrid recall: vector + FTS5 + reciprocal rank fusion."""
        t0 = time.monotonic()

        query_vec = embed_one(query, self.state_dir)
        vec_results = self._search_vec(
            query_vec, container_tag=container_tag, tier=tier,
            limit=limit * 2, temporal_range=temporal_range
        )
        fts_results = self._search_fts(
            query, container_tag=container_tag, limit=limit * 2
        )

        # Reciprocal rank fusion
        scores: dict[str, float] = {}
        sources: dict[str, str] = {}

        for rank, (mem_id, dist) in enumerate(vec_results):
            sim = 1.0 - (dist * dist / 2.0)  # L2 to cosine for normalized vecs
            if sim < SIMILARITY_THRESHOLD:
                continue
            rrf = 1.0 / (RRF_K + rank + 1)
            scores[mem_id] = scores.get(mem_id, 0) + rrf * 1.2  # boost vector
            sources[mem_id] = "vec"

        for rank, (mem_id, fts_rank) in enumerate(fts_results):
            rrf = 1.0 / (RRF_K + rank + 1)
            scores[mem_id] = scores.get(mem_id, 0) + rrf
            sources[mem_id] = "hybrid" if mem_id in sources else "fts"

        # Sort by combined score
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]

        # Fetch full memories
        db = self._get_db()
        scored_memories = []
        for mem_id, score in ranked:
            row = db.execute(
                "SELECT * FROM memories WHERE id = ? AND is_forgotten = 0",
                (mem_id,)
            ).fetchone()
            if not row:
                continue

            mem = self._row_to_memory(row)
            chain = []
            if include_version_chains and mem.parent_id:
                chain = self._get_version_chain(mem_id)

            scored_memories.append(ScoredMemory(
                memory=mem, score=score,
                source=sources.get(mem_id, "unknown"),
                version_chain=chain,
            ))

        result = RecallResult(
            memories=scored_memories,
            confidence=scored_memories[0].score if scored_memories else 0.0,
            timing_ms=(time.monotonic() - t0) * 1000,
        )

        if include_profile:
            result.profile_static, result.profile_dynamic = self._build_profile(
                container_tag=container_tag
            )

        return result

    # ── Profile ─────────────────────────────────────────────────────

    def _build_profile(
        self, container_tag: str | None = None
    ) -> tuple[list[str], list[str]]:
        """Build static + dynamic profile from memories."""
        db = self._get_db()

        where = "WHERE is_forgotten = 0 AND is_latest = 1"
        params: list[Any] = []
        if container_tag:
            where += " AND container_tag = ?"
            params.append(container_tag)

        # Static facts
        static_rows = db.execute(
            f"SELECT content FROM memories {where} AND is_static = 1 "
            "ORDER BY created_at DESC LIMIT 50",
            params
        ).fetchall()

        # Dynamic (recent non-static)
        dynamic_rows = db.execute(
            f"SELECT content FROM memories {where} AND is_static = 0 "
            "ORDER BY created_at DESC LIMIT 20",
            params
        ).fetchall()

        return (
            [r["content"] for r in static_rows],
            [r["content"] for r in dynamic_rows],
        )

    def profile(self, container_tag: str | None = None, query: str | None = None) -> RecallResult:
        """Get user profile, optionally with search results."""
        if query:
            result = self.recall(query, container_tag=container_tag, include_profile=True)
        else:
            static, dynamic = self._build_profile(container_tag)
            result = RecallResult(profile_static=static, profile_dynamic=dynamic)
        return result

    # ── Version chains ──────────────────────────────────────────────

    def _get_version_chain(self, memory_id: str, max_depth: int = 10) -> list[Memory]:
        """Follow parent_id chain to get version history."""
        db = self._get_db()
        chain = []
        current_id = memory_id
        for _ in range(max_depth):
            row = db.execute(
                "SELECT * FROM memories WHERE id = ?", (current_id,)
            ).fetchone()
            if not row or not row["parent_id"]:
                break
            parent_row = db.execute(
                "SELECT * FROM memories WHERE id = ?", (row["parent_id"],)
            ).fetchone()
            if not parent_row:
                break
            chain.append(self._row_to_memory(parent_row))
            current_id = parent_row["id"]
        return chain

    # ── Edge management ─────────────────────────────────────────────

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        confidence: float = 1.0,
    ) -> None:
        """Add a relationship edge between memories."""
        db = self._get_db()
        db.execute(
            "INSERT INTO memory_edges (source_id, target_id, edge_type, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_id, target_id, edge_type, confidence, _now())
        )
        db.commit()

    def get_edges(self, memory_id: str, edge_type: str | None = None) -> list[dict]:
        """Get all edges for a memory."""
        db = self._get_db()
        if edge_type:
            rows = db.execute(
                "SELECT * FROM memory_edges WHERE (source_id = ? OR target_id = ?) AND edge_type = ?",
                (memory_id, memory_id, edge_type)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM memory_edges WHERE source_id = ? OR target_id = ?",
                (memory_id, memory_id)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Internal search methods ─────────────────────────────────────

    def _vec_rowid_to_memory_id(self, vec_rowid: int) -> str | None:
        """Map a vec0 integer rowid back to the memory string ID."""
        db = self._get_db()
        row = db.execute(
            "SELECT memory_id FROM memory_vec_map WHERE rowid = ?", (vec_rowid,)
        ).fetchone()
        return row["memory_id"] if row else None

    def _find_similar(
        self,
        vec: list[float],
        container_tag: str | None = None,
        threshold: float = DEDUP_THRESHOLD,
        limit: int = 5,
    ) -> list[tuple[str, float]]:
        """Find similar memories by vector. Returns (memory_id, L2 distance) pairs."""
        db = self._get_db()

        # sqlite-vec returns L2 (Euclidean) distance.
        # For normalized vectors: cosine_sim = 1 - L2² / 2
        # So: L2 = sqrt(2 * (1 - cosine_sim))
        import math
        max_distance = math.sqrt(2.0 * (1.0 - threshold))

        rows = db.execute(
            "SELECT rowid, distance FROM memory_vec "
            "WHERE embedding MATCH ? AND k = ?",
            (_serialize_vec(vec), limit * 3)
        ).fetchall()

        results = []
        for row in rows:
            vec_rowid = row[0]
            dist = row[1]
            if dist > max_distance:
                continue

            mem_id = self._vec_rowid_to_memory_id(vec_rowid)
            if not mem_id:
                continue

            # Filter by container_tag if specified
            if container_tag:
                mem = db.execute(
                    "SELECT id FROM memories WHERE id = ? AND container_tag = ? "
                    "AND is_forgotten = 0",
                    (mem_id, container_tag)
                ).fetchone()
                if not mem:
                    continue
            else:
                mem = db.execute(
                    "SELECT id FROM memories WHERE id = ? AND is_forgotten = 0",
                    (mem_id,)
                ).fetchone()
                if not mem:
                    continue
            results.append((mem_id, dist))

        return results[:limit]

    def _search_vec(
        self,
        query_vec: list[float],
        container_tag: str | None = None,
        tier: str | None = None,
        limit: int = 40,
        temporal_range: tuple[str, str] | None = None,
    ) -> list[tuple[str, float]]:
        """Vector similarity search. Returns (memory_id, distance) pairs."""
        db = self._get_db()

        rows = db.execute(
            "SELECT rowid, distance FROM memory_vec "
            "WHERE embedding MATCH ? AND k = ?",
            (_serialize_vec(query_vec), limit * 3)
        ).fetchall()

        results = []
        for row in rows:
            vec_rowid = row[0]
            dist = row[1]

            mem_id = self._vec_rowid_to_memory_id(vec_rowid)
            if not mem_id:
                continue

            # Filter: container, tier, forgotten, temporal
            filters = ["is_forgotten = 0"]
            params: list[Any] = []

            if container_tag:
                filters.append("container_tag = ?")
                params.append(container_tag)
            if tier:
                filters.append("tier = ?")
                params.append(tier)
            if temporal_range:
                filters.append("event_date >= ? AND event_date <= ?")
                params.extend(temporal_range)

            where = " AND ".join(filters)
            mem = db.execute(
                f"SELECT id FROM memories WHERE id = ? AND {where}",
                (mem_id, *params)
            ).fetchone()
            if mem:
                results.append((mem_id, dist))
            if len(results) >= limit:
                break

        return results

    def _search_fts(
        self,
        query: str,
        container_tag: str | None = None,
        limit: int = 40,
    ) -> list[tuple[str, float]]:
        """FTS5 keyword search. Returns (memory_id, rank) pairs."""
        db = self._get_db()

        # Sanitize query for FTS5. Space-separated terms are implicit AND in
        # FTS5, which makes abstractive multi-term questions ("what did I buy
        # for my sister's birthday?") match nothing — every token, including
        # stopwords, would have to be present. Drop stopwords and use OR
        # (should-match) semantics; FTS5's BM25 `rank` then orders by relevance.
        raw = re.findall(r"[A-Za-z0-9]+", query.lower())
        content = [t for t in raw if t not in _FTS_STOPWORDS and len(t) > 1]
        terms = content or raw
        if not terms:
            return []
        fts_match = " OR ".join(f'"{t}"' for t in terms[:20])

        try:
            if container_tag:
                rows = db.execute(
                    "SELECT memory_id, rank FROM memory_fts "
                    "WHERE memory_fts MATCH ? AND container_tag = ? "
                    "ORDER BY rank LIMIT ?",
                    (fts_match, container_tag, limit)
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT memory_id, rank FROM memory_fts "
                    "WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fts_match, limit)
                ).fetchall()

            # Filter out forgotten
            results = []
            for row in rows:
                mem = db.execute(
                    "SELECT id FROM memories WHERE id = ? AND is_forgotten = 0",
                    (row[0],)
                ).fetchone()
                if mem:
                    results.append((row[0], row[1]))
            return results
        except Exception:
            return []

    # ── Helpers ─────────────────────────────────────────────────────

    def _row_to_memory(self, row: sqlite3.Row) -> Memory:
        """Convert a database row to a Memory dataclass."""
        return Memory(
            id=row["id"],
            content=row["content"],
            category=row["category"],
            tier=row["tier"],
            container_tag=row["container_tag"],
            is_static=bool(row["is_static"]),
            is_latest=bool(row["is_latest"]),
            is_forgotten=bool(row["is_forgotten"]),
            version=row["version"],
            parent_id=row["parent_id"],
            source_agent=row["source_agent"],
            source_conv=row["source_conv"],
            source_turn=row["source_turn"],
            event_date=row["event_date"],
            forget_after=row["forget_after"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def count(self, container_tag: str | None = None) -> int:
        """Count non-forgotten memories."""
        db = self._get_db()
        if container_tag:
            row = db.execute(
                "SELECT COUNT(*) as c FROM memories WHERE is_forgotten = 0 AND container_tag = ?",
                (container_tag,)
            ).fetchone()
        else:
            row = db.execute(
                "SELECT COUNT(*) as c FROM memories WHERE is_forgotten = 0"
            ).fetchone()
        return row["c"] if row else 0

    def get(self, memory_id: str) -> Memory | None:
        """Get a single memory by ID."""
        db = self._get_db()
        row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return self._row_to_memory(row) if row else None

    def close(self) -> None:
        """Close the database connection."""
        if self._db:
            self._db.close()
            self._db = None
