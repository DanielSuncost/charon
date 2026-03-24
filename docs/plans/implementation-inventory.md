# Charon Implementation Inventory

> Internal reference. What's built, where it lives, what it does.
> Updated: 2026-03-21 — 374 tests passing

## Core Daemon (`apps/core-daemon/`)

| File | Lines | What it does |
|------|-------|-------------|
| `conversation_engine.py` | ~480 | Multi-turn LLM loop. Streaming, tool calls, compaction, steering/follow-up queues, abort. |
| `system_prompt_builder.py` | ~400 | 10-layer prompt: identity, user model, project knowledge, working memory, goals, coordination, shade contracts, tools, context files, date/cwd. Injection scanning. |
| `charon_loop.py` | ~1100 | Daemon loop. Task queue processing, shade delegation, heartbeat, recurring tasks, autonomous self-assignment, boundary detection. |
| `agent_runtime.py` | ~650 | Task execution. Routes to ConversationEngine or heuristic. Intelligent summarization. Per-agent engine caching with prompt refresh. |
| `shade_orchestrator.py` | ~460 | Sequential shade contracts. Phase lifecycle, branch-from-failure, event logging. |
| `batch_orchestrator.py` | ~380 | Parallel shade swarms. Concurrent execution, per-task model routing, token tracking. |
| `autonomous.py` | ~400 | Goal states (proposed→confirmed→executing→verifying→completed). Self-assignment, goal inference from conversation. |
| `consolidation.py` | ~350 | Background user model updates. Reads interactions, extracts signals via LLM, applies changes. Trace storage. |
| `user_model_structured.py` | ~300 | 7-category user model. Load/save/render. Categories: style, coding, tooling, workflow, corrections, intentions, patterns. |
| `model_registry.py` | ~160 | Model tiers (fast/strong). Auto mode maps task complexity to tier. Per-shade provider config. |
| `task_summarizer.py` | ~200 | Fact-based summaries from tool calls. Fast mode (no LLM) + rich mode (LLM). |
| `task_ledger.py` | ~170 | Unified task history. Reads from queue + working memory, deduplicates, sorts. |
| `shade_stats.py` | ~130 | Token usage tracking. Per-agent, per-model, global totals. |
| `goal_runtime.py` | ~450 | Goal hierarchy. Idea capture, listing, promotion. Context packets. |
| `agent_lifecycle.py` | ~230 | Agent CRUD. Auto-naming, tmux sessions. SQLite sync. |
| `agent_policy.py` | ~70 | Shade delegation heuristics. |
| `boundary_runtime.py` | ~120 | Scope overlap detection, proposal/resolution. |
| `conversation_runtime.py` | ~400 | Task enqueueing (7 task types). Queue management. |
| `intervention_graph.py` | ~130 | Append-only message/intervention DAG. Path reconstruction. |
| `provider_bridge.py` | ~250 | Onboarding config → Provider + ModelInfo. Multi-provider resolution. |
| `conversation_store.py` | ~120 | JSONL conversation persistence. Save/load/list. |
| `store_adapter.py` | ~160 | SQLite singleton. Auto-migration from JSON. Thread-safe. |
| `user_model.py` | ~85 | Legacy user model stub (superseded by user_model_structured.py). |

## Tools (`apps/core-daemon/tools/`)

| File | Tool name | What it does |
|------|-----------|-------------|
| `__init__.py` | — | Registry, scope enforcement, execute_tool dispatcher |
| `memory_tools.py` | UserModel, ProjectKnowledge | Read/write to three-tier memory with injection scanning |
| `http_tool.py` | Http | HTTP requests via httpx |
| `git_tool.py` | Git | Structured git ops with agent metadata in commits |
| `search_tool.py` | Search | FTS5 conversation search |
| `batch_tool.py` | SpawnBatch | Create parallel shade swarms |
| `shade_tool.py` | SpawnShade | Create single sequential shade (built by other agent) |
| `dynamic_loader.py` | — | Plugin loader for .charon_state/tools/ and project tools |

## Providers (`apps/core-daemon/providers/`)

| File | What it does |
|------|-------------|
| `__init__.py` | Provider protocol, Message/ToolCall types, get_provider() |
| `httpx_openai.py` | Zero-dep streaming for LM Studio, Ollama, OpenRouter, OpenAI |
| `anthropic.py` | Anthropic Claude with thinking support |
| `openai_compat.py` | OpenAI SDK wrapper (fallback) |

## Storage (`libs/`)

| File | What it does |
|------|-------------|
| `store.py` | SQLite persistence. 14 tables, WAL mode. Full CRUD for agents, tasks, events, contracts, boundaries, goals, user model, run log, onboarding. Migration from JSON. |

## TUI (`apps/tui/opentui/`)

| File | What it does |
|------|-------------|
| `src/index.ts` | Main TUI. Chat/Dashboard/Sessions views. Key bindings. Status bar with heartbeat, mode, batch progress. |
| `src/backend.ts` | Python backend bridge (stdio JSON protocol) |
| `src/dashboard.ts` | Multi-column agent dashboard |
| `src/sessions.ts` | Session grid with live tmux capture |
| `chat_backend.py` | Python backend. Engine management, command handling, background worker thread, refresh payload. |

## Tests — 374 total

| Test file | Count | What it covers |
|-----------|-------|---------------|
| `test_store.py` | 49 | SQLite CRUD, migration |
| `test_tools.py` | ~30 | Built-in tool execution |
| `test_system_prompt_builder.py` | 25 | All 10 prompt layers |
| `test_memory_tools.py` | 26 | UserModel + ProjectKnowledge structured ops |
| `test_consolidation.py` | 14 | Config, triggers, signal collection, change application |
| `test_autonomous.py` | 14 | Goal lifecycle, self-assignment, budget |
| `test_task_summarizer.py` | 14 | Fast summarization from tool calls |
| `test_steering.py` | 8 | Steer/follow-up queues in engine |
| `test_ideas_and_heartbeat.py` | 8 | Idea capture, heartbeat emission |
| `test_store_adapter.py` | 8 | Singleton DB, auto-migration |
| `test_task_ledger.py` | 7 | History from tasks + memory, dedup |
| `test_sqlite_integration.py` | 6 | End-to-end SQLite wiring |
| `test_scope_search_queue.py` | 16 | Shade scope enforcement, FTS5 search |
| `test_http_git_recurring.py` | 16 | Http/Git tools, recurring tasks |
| `test_dynamic_tools.py` | 12 | Plugin loading, execution, conflicts |
| `test_model_registry_and_batch.py` | 11 | Model tiers, batch lifecycle |
| `test_charon_loop*.py` | ~60 | Daemon loop, shade orchestration, boundaries, interventions |
| Others | ~50 | Agent lifecycle, conversation runtime, providers, etc. |

## Config files in `.charon_state/`

| File | What it stores |
|------|---------------|
| `onboarding.json` | Provider, model, project, setup state |
| `user_model.json` | Structured user profile (7 categories) |
| `USER.md` | Human-readable user profile export |
| `model_registry.json` | Shade model config (tiers, provider, mode) |
| `autonomous_config.json` | Autonomous mode settings |
| `consolidation_config.json` | Consolidation scan settings |
| `agents.json` | Agent records |
| `queue.json` | Task queue |
| `shade_contracts.json` | Sequential shade contracts |
| `shade_batches.json` | Parallel batch records |
| `charon.db` | SQLite database (all of the above + more) |
| `conversations/<id>.jsonl` | Per-agent conversation history |
| `agents/<id>/working_memory.json` | Per-agent working memory |
| `agents/<id>/inbox.jsonl` | Per-agent event inbox |
| `goals/projects/<id>.json` | Per-project goal hierarchy |
| `auth/auth.json` | OAuth tokens |
