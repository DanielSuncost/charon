# Semantic Memory Engine

## Goal

Build a local-first semantic memory engine that scores competitively on
LongMemEval_S (cloud-hosted SOTA: 81.6%). No cloud, no API keys, no
vector DB — SQLite + local embeddings only.

## What LongMemEval_S Tests

500 questions across 6 categories, evaluated over ~48 sessions of chat
history (~122K tokens per question):

| Category | Count | What it tests |
|----------|-------|--------------|
| single-session-user | 70 | Recall a fact the user mentioned once |
| single-session-assistant | 56 | Recall something the assistant said |
| single-session-preference | 30 | Apply a preference the user expressed |
| multi-session | 133 | Combine facts from 2-4 different sessions |
| temporal-reasoning | 133 | Reason about time ordering/duration between events |
| knowledge-update | 78 | Track fact changes (old value → new value) |

Supermemory's weakness: temporal reasoning and knowledge updates require
**relationship tracking** between memories, which their graph handles but
imperfectly. We can do better with explicit version chains and timestamps.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    Memory Engine (Python)                      │
│                                                                │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │  Extractor   │  │  Embedder    │  │  Relationship        │ │
│  │  (LLM-based) │  │  (local      │  │  Tracker             │ │
│  │              │  │  MiniLM-L6)  │  │  (updates/extends/   │ │
│  │              │  │              │  │   derives/temporal)   │ │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬───────────┘ │
│         │                 │                      │             │
│  ┌──────┴─────────────────┴──────────────────────┴───────────┐ │
│  │                    SQLite + sqlite-vec                      │ │
│  │                                                             │ │
│  │  memories        — id, content, category, is_static,        │ │
│  │                    is_latest, version, parent_id,            │ │
│  │                    forget_after, created_at, source_*        │ │
│  │  memory_vec      — vec0 virtual table (384-dim float)       │ │
│  │  memory_edges    — source_id, target_id, edge_type,         │ │
│  │                    confidence, created_at                    │ │
│  │  memory_fts      — FTS5 for keyword fallback                │ │
│  │  conversation_fts — (existing) for raw search               │ │
│  └─────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

### 1. Hybrid retrieval: vector + FTS5 + temporal filtering

For each recall query:
1. **Vector search** (sqlite-vec): cosine similarity top-K
2. **FTS5 search**: keyword match top-K  
3. **Merge + rerank**: RRF (Reciprocal Rank Fusion) combines both lists
4. **Temporal filter**: if query has temporal markers, narrow search space
5. **Relationship expansion**: follow edges to get version chains

This beats pure vector (Supermemory) on keyword-heavy queries and beats
pure FTS5 (current Charon) on semantic queries.

### 2. Fact extraction at ingest time (not just search time)

When a conversation session ends, extract structured facts:
- **User facts**: preferences, biographical details, project info
- **Temporal events**: things that happened, with dates
- **Knowledge claims**: things stated as true (can be updated later)

Each fact becomes a memory with metadata. The LLM extracts them in a
single pass using a structured prompt.

### 3. Version chains for knowledge updates

When a new fact contradicts an existing one:
- Link them with an `updates` edge
- Mark the old one `is_latest=false`
- The new one becomes `is_latest=true`
- Both are retrievable, but the latest is ranked higher

This directly targets LongMemEval's knowledge-update category.

### 4. Temporal indexing

Every memory gets:
- `created_at`: when it was stored
- `event_date`: when the event happened (extracted by LLM)
- `forget_after`: optional expiry

Temporal queries ("how many days between X and Y") can filter by
event_date range before doing similarity search.

### 5. Local embedding model

`all-MiniLM-L6-v2`: 22M params, 384 dims, runs on CPU in ~5ms per
embedding. Good enough for <10K memories. If we need better quality,
upgrade to `bge-small-en-v1.5` (33M, 384 dims) or
`gte-small` (33M, 384 dims).

### 6. Abstention detection

LongMemEval tests whether the system correctly says "I don't know."
Our recall function returns confidence scores. Below a threshold,
the agent should abstain rather than hallucinate.

## Schema

```sql
CREATE TABLE memories (
    id            TEXT PRIMARY KEY,
    content       TEXT NOT NULL,
    category      TEXT NOT NULL DEFAULT 'general',
    tier          TEXT NOT NULL DEFAULT 'user',  -- user/project/agent
    container_tag TEXT NOT NULL DEFAULT 'default',
    is_static     INTEGER NOT NULL DEFAULT 0,
    is_latest     INTEGER NOT NULL DEFAULT 1,
    is_forgotten  INTEGER NOT NULL DEFAULT 0,
    version       INTEGER NOT NULL DEFAULT 1,
    parent_id     TEXT,
    source_agent  TEXT,
    source_conv   TEXT,
    source_turn   INTEGER,
    event_date    TEXT,           -- when the event happened
    forget_after  TEXT,           -- auto-expiry timestamp
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES memories(id)
);

CREATE VIRTUAL TABLE memory_vec USING vec0(
    embedding float[384]
);

CREATE TABLE memory_edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    edge_type   TEXT NOT NULL,   -- updates, extends, derives, temporal_before
    confidence  REAL NOT NULL DEFAULT 1.0,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES memories(id),
    FOREIGN KEY (target_id) REFERENCES memories(id)
);

CREATE VIRTUAL TABLE memory_fts USING fts5(
    id, content, category, container_tag,
    tokenize='porter unicode61'
);

CREATE INDEX idx_mem_container ON memories(container_tag);
CREATE INDEX idx_mem_tier ON memories(tier);
CREATE INDEX idx_mem_latest ON memories(is_latest);
CREATE INDEX idx_mem_parent ON memories(parent_id);
CREATE INDEX idx_mem_event_date ON memories(event_date);
CREATE INDEX idx_mem_forgotten ON memories(is_forgotten);
CREATE INDEX idx_edges_source ON memory_edges(source_id);
CREATE INDEX idx_edges_target ON memory_edges(target_id);
CREATE INDEX idx_edges_type ON memory_edges(edge_type);
```

## Extraction Prompt

```
Extract facts from this conversation session. For each fact, output JSON:

{
  "facts": [
    {
      "content": "the fact as a single clear sentence",
      "category": "biographical|preference|event|knowledge|project|relationship",
      "is_static": true/false,  // true = permanent fact, false = may change
      "event_date": "YYYY-MM-DD" or null,  // when this happened
      "temporal_markers": ["before X", "after Y"] or [],
      "supersedes": "text of the old fact this replaces" or null
    }
  ]
}

Rules:
- Extract only facts generalizable beyond this conversation
- Include the user's preferences, biographical details, activities
- Include specific details (numbers, names, dates, durations)
- If a fact updates a previous one, set supersedes to the old version
- Set event_date when the user mentions when something happened
- Skip assistant-side trivia and generic advice
```

## Recall API

```python
def recall(
    query: str,
    container_tag: str = "default",
    tier: str | None = None,
    limit: int = 20,
    include_profile: bool = True,
    temporal_range: tuple[str, str] | None = None,
) -> RecallResult:
    """
    Returns:
      - memories: list of matching memories with scores
      - profile: static + dynamic facts (if include_profile)
      - confidence: overall confidence (for abstention)
    """
```

## Integration with Charon's Three Tiers

The semantic engine **enhances** the existing tiers, it doesn't replace them:

| Tier | Before | After |
|------|--------|-------|
| User Model | Structured 7-category dict | Same + vector-indexed memories |
| Project Knowledge | Flat markdown | Same + vector-indexed project facts |
| Working Memory | Task summaries (JSON) | Same + vector-indexed task facts |
| Conversation Search | FTS5 only | Hybrid vector + FTS5 |

The system prompt builder continues to use frozen snapshots from the
structured model. The semantic engine adds a **recall tool** that agents
use when they need deeper/broader memory access.

## LongMemEval_S Benchmark Runner

To benchmark:
1. For each of the 500 questions, feed haystack sessions through the
   extraction pipeline (facts → embeddings → memories table)
2. Ask the question using the recall API + LLM reader
3. Output hypothesis JSONL
4. Evaluate with LongMemEval's evaluate_qa.py

## Implementation Order

1. `apps/core-daemon/memory_engine.py` — schema, CRUD, embedding, search
2. `apps/core-daemon/memory_extractor.py` — LLM fact extraction
3. `apps/core-daemon/memory_relations.py` — edge management, version chains
4. `apps/core-daemon/tools/recall_tool.py` — agent-facing recall tool
5. `tests/test_memory_engine.py` — unit tests
6. `scripts/bench_longmemeval.py` — benchmark runner
7. Integration into `charon_loop.py` — extract facts after each session
