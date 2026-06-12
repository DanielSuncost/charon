# Semantic Memory Engine

## Goal

Build a local-first semantic memory engine that scores well on
LongMemEval_S using only on-device resources. No cloud, no API keys,
no vector DB — SQLite + local embeddings only.

Current score: 78.8% with GPT-4o as the responder model, above the
original paper's best RAG configuration (72%). Note: LongMemEval
scores depend heavily on the responder model — the same retrieval
system can swing from ~84% to ~95% by switching from GPT-4o to
GPT-5-mini. The value here is achieving competitive recall with ~5ms
retrieval latency on local hardware and no cloud dependencies.

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

## LongMemEval_S Benchmark

### Results

| Responder model | Score | Date |
|-----------------|-------|------|
| GPT-4o (via OpenRouter) | 78.8% | 2026-03 |

Retrieval accuracy: R@1 0.72, R@5 0.95, R@10 0.985 (500 questions).

Note: LongMemEval scores depend heavily on the responder model. The
same retrieval system can produce very different scores with different
readers (e.g. Mastra reports 84.2% with GPT-4o vs 94.9% with
GPT-5-mini on the same retrieval). Our retrieval runs entirely locally
(bge-base-en-v1.5 embeddings, SQLite + sqlite-vec, ~5ms per recall).

### Reproducing

The benchmark has two stages: retrieval (local, slow) and reading
(API, fast). Scripts are in `scripts/`.

```bash
# Stage 1: Retrieval only (slow — indexes all sessions per question)
PYTHONPATH=apps/core-daemon CHARON_EMBED_BACKEND=local \
  python scripts/bench_longmemeval.py --retrieval-only

# Stage 2: Reader with a specific model (fast — uses saved retrieval)
OPENROUTER_API_KEY=sk-or-... PYTHONPATH=apps/core-daemon \
  python scripts/bench_longmemeval.py \
    --reader-provider openrouter \
    --reader-model openai/gpt-4o \
    --retrieval-file results/longmemeval/retrieval_*.json

# Evaluate (uses GPT-4o as judge, matching LongMemEval's official eval)
OPENROUTER_API_KEY=sk-or-... \
  python scripts/eval_longmemeval.py \
    results/longmemeval/hyp_*.jsonl
```

The retrieval stage downloads the LongMemEval_S dataset automatically
on first run (~277MB). Results are saved incrementally so interrupted
runs can resume.

## Implementation Order

1. `apps/core-daemon/memory_engine.py` — schema, CRUD, embedding, search
2. `apps/core-daemon/memory_extractor.py` — LLM fact extraction
3. `apps/core-daemon/memory_relations.py` — edge management, version chains
4. `apps/core-daemon/tools/recall_tool.py` — agent-facing recall tool
5. `tests/test_memory_engine.py` — unit tests
6. `scripts/bench_longmemeval.py` — benchmark runner
7. Integration into `charon_loop.py` — extract facts after each session
