# Charon Master Plan: From Scattered Stubs to Publishable System

> Date: 2026-03-18
> Status: Active — the single source of truth for what to build and in what order.

---

## 1. What Charon Actually Is (One Paragraph)

Charon is a single-user agent operating system that lets you interact with coding agent sessions running anywhere — local or remote — without SSH/tmux juggling. It provides: (1) a persistent agent runtime with durable memory and goal tracking, (2) internal "Shade" workers that decompose tasks with contract-based orchestration, (3) a beautiful OpenTUI terminal frontend with chat, dashboard, and session-grid views, and (4) a `charons-boat` adapter that bridges any existing agent framework (pi-agent, hermes, etc.) into the Charon network via tmux+SSH tunnels.

---

## 2. Honest Assessment: What We Have vs. What We Need

### What exists and works (✅)
- **Core loop** (`charon_loop.py`, 993 lines): F00 persistent loop with task queue, retry, idle, stop-file. Solid. 59 tests passing.
- **Shade orchestrator** (`shade_orchestrator.py`, 400 lines): Contract creation, phase lifecycle, branch-from-phase, event logging. Well-structured.
- **Agent runtime** (`agent_runtime.py`, 339 lines): Task tick execution, shell/write_file actions, working memory, attempt tracking.
- **Agent lifecycle** (`agent_lifecycle.py`, 210 lines): Create/list agents, tmux session management.
- **Intervention graph** (`intervention_graph.py`, 129 lines): Append-only message/intervention graph with conversation threading.
- **Conversation runtime/index**: Thread listing, index rebuilding.
- **Goal runtime** (`goal_runtime.py`, 288 lines): Project/session/goal hierarchy, intent ingestion, context packets.
- **Boundary runtime** (`boundary_runtime.py`, 104 lines): Scope overlap detection, proposal/resolution flow.
- **LLM adapter** (`llm_adapter.py`, 156 lines): LM Studio integration, model detection from onboarding.
- **Agent policy** (`agent_policy.py`, 67 lines): Shade delegation heuristics.
- **JSON schemas**: agent, task, event, node-link, rlm-node (with valid/invalid test fixtures).
- **Command contracts doc**: `/agent create/list/assign/task/link/inbox/thread/intervene/backtrack`.
- **OpenTUI prototype**: Bun frontend stub + Python backend bridge — renders title art, handles `/setup` commands.
- **Textual TUI**: Onboarding screen, mascot preview, chat stub.
- **Feature docs**: F00–F52 cards (all "planned", boilerplate steps).
- **Test suite**: 59 tests, 1.6s, all green.

### What's missing or broken (❌)

| Gap | Severity | Notes |
|-----|----------|-------|
| **No real LLM agent loop** | Critical | `decide_action` is a heuristic stub. No multi-turn conversation, no tool-use loop, no context window management. This is the entire point. |
| **No `charons-boat` bridge tool** | Critical | The USP (connect any agent framework to Charon via tmux) doesn't exist at all. |
| **No remote linking** | Critical | No transport, no auth handshake, no heartbeat, no remote command dispatch. |
| **No working frontend** | High | OpenTUI has a mascot renderer and a text input. No chat view, no dashboard, no session-grid. Textual TUI is similarly skeletal. |
| **No session-grid view** | High | Core navigation concept, zero implementation. |
| **No persistence layer** | High | Everything is JSON files with no locking, no indexing, no concurrent-access safety. |
| **No real tool system** | High | Only `shell` and `write_file`. No `read`, no `edit`, no `search`, no `bash` with proper PTY. |
| **No user preference / memory system** | Medium | `user_model.py` is 83 lines of stubs. No learned preferences, no consolidation. |
| **No project registry** | Medium | F01 multi-project is "planned" only. |
| **Feature cards are boilerplate** | Low | 53 feature cards with identical template text. Not actionable specs. |
| **No CI/CD** | Low | No GitHub Actions, no lint, no type-check. |
| **No packaging** | Low | No `pyproject.toml`, no CLI entry point, no `npm publish` setup. |

---

## 3. Architecture Decisions (Locked)

### A1: Runtime language split
- **Core daemon + agent logic**: Python (asyncio where needed, sync for loop simplicity).
- **Frontend**: Bun + TypeScript + OpenTUI. Single process, talks to backend via newline-delimited JSON over stdio or Unix socket.
- **`charons-boat` bridge**: Lightweight Python/shell script installed inside any agent framework's tmux session.

### A2: Storage
- **V1**: SQLite for structured data (agents, tasks, queue, goals, events). JSONL stays for append-only event logs only.
- **Reasoning**: JSON files with no locking will corrupt under concurrent shade workers. SQLite gives us WAL mode, transactions, and indexing for free.

### A3: Transport for remote linking
- **V1**: WebSocket + JSON-RPC over SSH tunnel (reuse existing SSH keys). Polling fallback for degraded mode.
- **No custom auth protocol in V1** — SSH key auth is already solved.

### A4: Agent execution model
- **Primary agent loop**: Inspired by pi-agent/hermes. Multi-turn conversation with the LLM, structured tool calls, context window management with compaction.
- **Tools**: Read, Write, Edit, Bash (with PTY capture), Search (ripgrep).
- **Shade execution**: Same tool set, scoped to contract constraints, budget-limited.

### A5: Frontend architecture
- **Three views**, switchable with tabs/hotkeys:
  1. **Chat**: Full agent conversation (like pi-agent). Input at bottom, streaming response, tool call display.
  2. **Dashboard**: Agent cards showing status, project, current task, health. Connected remote nodes.
  3. **Session Grid**: All charon-enabled sessions across all locations. Filterable by project.

### A6: `charons-boat` bridge design
- Installed in any agent framework's tmux session.
- Opens a Unix socket (or named pipe) that Charon can connect to.
- For remote: SSH tunnel to forward the socket. Charon manages the tunnel lifecycle silently.
- Protocol: Simple JSON-RPC. Commands: `status`, `send_input`, `get_output`, `get_screen`.

---

## 4. Build Phases (Sequential, Each Shippable)

### Phase 1: Foundation (the "it actually works" release)
**Goal**: A single local Charon agent that can have a real multi-turn conversation with an LLM, use tools, and persist its state. No UI frills, just the engine.

| Task | Module | Est. |
|------|--------|------|
| 1.1 SQLite persistence layer | `libs/store.py` | 2d |
| 1.2 Multi-turn conversation engine | `apps/core-daemon/conversation_engine.py` | 3d |
| 1.3 Tool system (read, write, edit, bash, search) | `apps/core-daemon/tools/` | 2d |
| 1.4 Context window management + compaction | `apps/core-daemon/context.py` | 2d |
| 1.5 LLM provider abstraction (Anthropic, OpenAI, local) | `apps/core-daemon/providers/` | 1d |
| 1.6 Wire into existing charon_loop | `apps/core-daemon/charon_loop.py` | 1d |
| 1.7 CLI chat mode (no TUI, just stdin/stdout) | `scripts/charon_chat.py` | 1d |
| 1.8 Tests for conversation + tools | `tests/` | 1d |

**Exit criteria**: `python scripts/charon_chat.py` lets you have a coding conversation that reads files, makes edits, runs commands, and remembers what it did across turns.

### Phase 2: Agent Lifecycle + Shade Orchestration
**Goal**: Multiple named agents, project assignment, automatic shade delegation.

| Task | Module | Est. |
|------|--------|------|
| 2.1 Migrate agent/task data to SQLite | `libs/store.py` | 1d |
| 2.2 Agent CRUD with proper state machine | `apps/core-daemon/agent_lifecycle.py` | 1d |
| 2.3 `/agent` command router | `apps/core-daemon/commands.py` | 1d |
| 2.4 Shade delegation with real LLM execution | `apps/core-daemon/shade_orchestrator.py` | 2d |
| 2.5 Contract branch-and-resume with LLM | `apps/core-daemon/shade_orchestrator.py` | 1d |
| 2.6 Goal runtime integration with conversation | `apps/core-daemon/goal_runtime.py` | 1d |
| 2.7 Integration tests: multi-agent scenarios | `tests/` | 1d |

**Exit criteria**: You can create two agents assigned to different projects, give each tasks, watch shades execute phases, and see results flow back.

### Phase 3: OpenTUI Frontend — Chat View
**Goal**: A beautiful terminal chat experience that replaces the CLI mode.

| Task | Module | Est. |
|------|--------|------|
| 3.1 Backend protocol expansion (streaming, tool display) | `apps/tui/opentui/opentui_backend.py` | 2d |
| 3.2 Chat view component (message list, streaming, markdown) | `apps/tui/opentui/src/views/chat.ts` | 3d |
| 3.3 Input bar with command completion | `apps/tui/opentui/src/components/input.ts` | 1d |
| 3.4 Tool call display (collapsible, syntax-highlighted) | `apps/tui/opentui/src/components/tool_call.ts` | 1d |
| 3.5 Status bar (model, tokens, agent, project) | `apps/tui/opentui/src/components/status_bar.ts` | 0.5d |
| 3.6 Onboarding flow in OpenTUI | `apps/tui/opentui/src/views/onboarding.ts` | 1d |

**Exit criteria**: `bun run start` opens a terminal app where you can chat with Charon, see tool calls inline, and it looks good.

### Phase 4: Dashboard + Session Grid
**Goal**: See all your agents and sessions at a glance.

| Task | Module | Est. |
|------|--------|------|
| 4.1 Dashboard view — agent cards with live status | `apps/tui/opentui/src/views/dashboard.ts` | 2d |
| 4.2 Session grid view — all sessions, filterable | `apps/tui/opentui/src/views/session_grid.ts` | 2d |
| 4.3 View switcher (tabs/hotkeys) | `apps/tui/opentui/src/layout/tabs.ts` | 1d |
| 4.4 Backend: session discovery (local tmux scan) | `apps/core-daemon/session_discovery.py` | 1d |
| 4.5 Agent detail panel (inbox, thread, memory) | `apps/tui/opentui/src/views/agent_detail.ts` | 1d |

**Exit criteria**: Three-tab TUI: Chat / Dashboard / Sessions. Dashboard shows agent cards. Sessions shows all tmux sessions with charon-boat installed.

### Phase 5: `charons-boat` Bridge
**Goal**: Connect any agent framework running in tmux to Charon.

| Task | Module | Est. |
|------|--------|------|
| 5.1 `charons-boat` agent-side daemon | `tools/charons-boat/boat.py` | 2d |
| 5.2 Protocol: JSON-RPC over Unix socket | `tools/charons-boat/protocol.py` | 1d |
| 5.3 Charon-side connector (local) | `apps/core-daemon/boat_connector.py` | 1d |
| 5.4 tmux screen capture + input injection | `tools/charons-boat/tmux_bridge.py` | 1d |
| 5.5 Display external sessions in session grid | `apps/tui/opentui/src/views/session_grid.ts` | 1d |
| 5.6 Interactive session viewer (view/send input) | `apps/tui/opentui/src/views/session_viewer.ts` | 2d |

**Exit criteria**: Install `charons-boat` in a pi-agent tmux session. It shows up in Charon's session grid. You can view its output and send it input from Charon.

### Phase 6: Remote Linking
**Goal**: Control agents on remote servers without manual SSH.

| Task | Module | Est. |
|------|--------|------|
| 6.1 SSH tunnel manager | `apps/core-daemon/tunnel_manager.py` | 2d |
| 6.2 Node registry (trusted remotes) | `apps/core-daemon/node_registry.py` | 1d |
| 6.3 Remote command dispatch over tunnel | `apps/core-daemon/remote_dispatch.py` | 2d |
| 6.4 Heartbeat + status sync | `apps/core-daemon/heartbeat.py` | 1d |
| 6.5 Dashboard: remote node cards | `apps/tui/opentui/src/views/dashboard.ts` | 1d |
| 6.6 Session grid: remote sessions | `apps/tui/opentui/src/views/session_grid.ts` | 1d |
| 6.7 `/agent link add/list/revoke` commands | `apps/core-daemon/commands.py` | 1d |

**Exit criteria**: `charon link add user@server` enrolls a remote. Its agents and sessions appear in dashboard/grid. You can issue tasks to remote agents from local Charon.

### Phase 7: Memory, Learning, Polish
**Goal**: The agent gets smarter over time and the product feels complete.

| Task | Module | Est. |
|------|--------|------|
| 7.1 User preference model | `apps/core-daemon/user_model.py` | 2d |
| 7.2 Memory retention + consolidation | `apps/core-daemon/memory.py` | 2d |
| 7.3 Context packet enrichment for conversations | `apps/core-daemon/context.py` | 1d |
| 7.4 Theme system (vintage terminal aesthetics) | `apps/tui/opentui/src/themes/` | 1d |
| 7.5 Health-colored frames + tok/s display | `apps/tui/opentui/src/components/` | 1d |
| 7.6 Rear-view mirror (recent activity ticker) | `apps/tui/opentui/src/components/rearview.ts` | 0.5d |
| 7.7 Packaging: pyproject.toml, CLI entry, npm publish | root | 1d |
| 7.8 CI: GitHub Actions (lint, test, type-check) | `.github/workflows/` | 0.5d |
| 7.9 README rewrite for public launch | `README.md` | 0.5d |

**Exit criteria**: Publishable. `pip install charon-agent` or `npm install -g charon-agent`. Clean README with screenshots.

---

## 5. Directory Structure (Target)

```
charon/
├── apps/
│   ├── core-daemon/
│   │   ├── charon_loop.py          # Main orchestration loop
│   │   ├── conversation_engine.py  # Multi-turn LLM conversation  [NEW]
│   │   ├── context.py              # Context window + compaction   [NEW]
│   │   ├── commands.py             # /agent command router         [NEW]
│   │   ├── providers/              # LLM provider abstraction      [NEW]
│   │   │   ├── anthropic.py
│   │   │   ├── openai.py
│   │   │   └── local.py
│   │   ├── tools/                  # Agent tool implementations    [NEW]
│   │   │   ├── read.py
│   │   │   ├── write.py
│   │   │   ├── edit.py
│   │   │   ├── bash.py
│   │   │   └── search.py
│   │   ├── agent_lifecycle.py      # Agent CRUD + state machine
│   │   ├── agent_runtime.py        # Task execution engine
│   │   ├── agent_policy.py         # Shade delegation policy
│   │   ├── shade_orchestrator.py   # Contract lifecycle
│   │   ├── goal_runtime.py         # Goal hierarchy
│   │   ├── intervention_graph.py   # Conversation threading
│   │   ├── conversation_runtime.py # Thread management
│   │   ├── conversation_index.py   # Thread indexing
│   │   ├── boundary_runtime.py     # Scope overlap resolution
│   │   ├── session_discovery.py    # Discover local/remote sessions [NEW]
│   │   ├── boat_connector.py       # Connect to charons-boat        [NEW]
│   │   ├── tunnel_manager.py       # SSH tunnel lifecycle           [NEW]
│   │   ├── node_registry.py        # Remote node trust registry     [NEW]
│   │   ├── remote_dispatch.py      # Remote command dispatch        [NEW]
│   │   ├── heartbeat.py            # Remote status sync             [NEW]
│   │   ├── memory.py               # Memory retention/consolidation [NEW]
│   │   ├── user_model.py           # User preferences
│   │   ├── llm_adapter.py          # Legacy LM Studio adapter
│   │   └── charon_auth.py          # Auth helpers
│   └── tui/
│       └── opentui/
│           ├── src/
│           │   ├── index.ts         # Entry point
│           │   ├── layout/
│           │   │   └── tabs.ts      # View switcher               [NEW]
│           │   ├── views/
│           │   │   ├── chat.ts      # Chat conversation view      [NEW]
│           │   │   ├── dashboard.ts # Agent dashboard             [NEW]
│           │   │   ├── session_grid.ts # Session grid             [NEW]
│           │   │   ├── session_viewer.ts # Interactive session    [NEW]
│           │   │   ├── agent_detail.ts # Agent detail panel       [NEW]
│           │   │   └── onboarding.ts # First-run setup           [NEW]
│           │   ├── components/
│           │   │   ├── input.ts     # Enhanced input bar          [NEW]
│           │   │   ├── tool_call.ts # Tool call display           [NEW]
│           │   │   ├── status_bar.ts # Status bar                 [NEW]
│           │   │   ├── rearview.ts  # Recent activity ticker      [NEW]
│           │   │   └── agent_card.ts # Agent card component       [NEW]
│           │   └── themes/
│           │       └── vintage.ts   # Theme definitions           [NEW]
│           ├── opentui_backend.py   # Python bridge process
│           └── package.json
├── tools/
│   └── charons-boat/               # Bridge tool                  [NEW]
│       ├── boat.py                  # Agent-side daemon
│       ├── protocol.py              # JSON-RPC protocol
│       └── tmux_bridge.py           # tmux screen/input bridge
├── libs/
│   └── store.py                     # SQLite persistence layer    [NEW]
├── docs/
│   ├── contracts/                   # Schema files (existing)
│   ├── features/                    # Feature cards (existing)
│   └── plans/
│       └── MASTER_PLAN.md           # This file
├── tests/                           # Test suite (existing + new)
├── scripts/
│   ├── charon_chat.py               # CLI chat mode               [NEW]
│   └── ...
├── pyproject.toml                   # Python packaging            [NEW]
└── package.json
```

---

## 6. Key Design Contracts

### 6.1 Conversation Engine Protocol
```
ConversationEngine:
  - start_turn(user_message: str) -> stream[AssistantChunk]
  - AssistantChunk = TextDelta | ToolCall | ToolResult | TurnComplete
  - Manages context window: system prompt + memory + recent messages
  - Compaction: when context exceeds threshold, summarize older turns
  - Persists all turns to SQLite (never loses conversation history)
```

### 6.2 Tool Interface
```python
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema
    
    def execute(self, params: dict, context: ToolContext) -> ToolResult
    
ToolContext:
    project_root: Path
    agent_id: str
    budget: Budget  # token/time limits for shade execution
    
ToolResult:
    success: bool
    output: str
    truncated: bool
```

### 6.3 Backend ↔ Frontend Protocol (expanded)
```
Frontend → Backend:
  { type: "chat", message: "...", request_id: "..." }
  { type: "command", command: "/agent list", request_id: "..." }
  { type: "refresh", request_id: "..." }
  { type: "session_action", session_id: "...", action: "view"|"send", input?: "..." }

Backend → Frontend:
  { type: "chat_delta", text: "...", request_id: "..." }
  { type: "tool_call", tool: "bash", params: {...}, request_id: "..." }
  { type: "tool_result", output: "...", success: true, request_id: "..." }
  { type: "turn_complete", request_id: "..." }
  { type: "refresh", payload: { agents, sessions, queue, ... }, request_id: "..." }
  { type: "session_output", session_id: "...", lines: [...] }
```

### 6.4 `charons-boat` Protocol
```
Charon → Boat (over Unix socket / SSH tunnel):
  { method: "status" }                    → { agent_type, status, project }
  { method: "get_screen", lines: 50 }     → { screen: [...lines] }
  { method: "send_input", text: "..." }   → { ok: true }
  { method: "get_metadata" }              → { name, framework, uptime, ... }

Boat listens on: ~/.charon/boat.sock (local) or tunneled via SSH
```

---

## 7. What to Kill

These should be removed or archived to reduce confusion:

1. **`apps/tui/charon_textual.py` + `charon_tui.py`**: Textual TUI is abandoned in favor of OpenTUI. Archive to `archive/`.
2. **`apps/tui/pty_widget.py` + `tmux_widget.py`**: Textual-specific widgets, no longer needed.
3. **`apps/tui/mascot_preview.py`**: Standalone script, keep in `scripts/` if useful.
4. **`delegation/DELEGATION_MATRIX.md`**: Stale delegation concept, merge relevant ideas into shade orchestrator.
5. **Feature cards F06-F49**: Most are single-sentence placeholders. Consolidate into this plan's phases. Keep F00, F50-F52 as reference.
6. **`docs/plans/14-day-sprint.md`**: Superseded by this plan.
7. **`scripts/charon_delegate.py`**: Superseded by the agent task system.

---

## 8. Principles for Implementation

1. **Each phase ships something usable.** No "infrastructure week" that produces nothing visible.
2. **Tests before features.** Every module gets unit tests on creation. Integration tests per phase.
3. **SQLite with WAL mode everywhere.** No more JSON file races.
4. **Streaming first.** The conversation engine streams from day one. No "add streaming later."
5. **Error boundaries.** Every external call (LLM, SSH, subprocess) has timeout + retry + graceful degradation.
6. **Budget enforcement.** Shade contracts have token and time budgets. Enforced, not advisory.
7. **The user talks to agents, never to shades.** This invariant is tested, not just documented.
8. **Clean module boundaries.** No `importlib.util.spec_from_file_location` hacks. Use proper Python packages with `__init__.py`.

---

## 9. Progress Log

### Phase 1.1: SQLite persistence layer ✅ COMPLETE
- `libs/store.py` — 880 lines, 14 tables, WAL mode, full CRUD + migration from JSON
- `tests/test_store.py` — 49 tests, all passing

### Phase 1.2: Conversation engine + tools + providers ✅ COMPLETE
- `apps/core-daemon/conversation_engine.py` — Multi-turn agent loop with streaming, tool use, compaction
- `apps/core-daemon/tools/__init__.py` — Read, Write, Edit, Bash tools (pi-agent parity)
- `apps/core-daemon/providers/__init__.py` — Provider abstraction with Anthropic + OpenAI-compatible
- `apps/core-daemon/providers/anthropic.py` — Full streaming with thinking support
- `apps/core-daemon/providers/openai_compat.py` — Works with OpenAI, LM Studio, Ollama
- `scripts/charon_chat.py` — CLI chat mode with colored output and tool display
- `tests/test_tools.py` — 29 tests
- `tests/test_conversation_engine.py` — 22 tests (mock provider, full loop verification)
- **Total: 159 tests passing in 3.3s**

### Phase 1.3+: SQLite wiring + system prompt + memory ✅ COMPLETE
- `apps/core-daemon/store_adapter.py` — Singleton DB with auto-migration from JSON
- All 7 runtime modules wired for dual-write (SQLite + JSON fallback)
- `apps/core-daemon/system_prompt_builder.py` — 10-layer prompt assembly
- `apps/core-daemon/user_model_structured.py` — 7-category user model
- `apps/core-daemon/consolidation.py` — Background LLM analysis of user interactions
- 8 + 6 + 25 + 26 + 14 integration/unit tests

### Phase 2: Agent orchestration + shade swarms ✅ COMPLETE
- `apps/core-daemon/autonomous.py` — Goal states, confirmation flow, self-assignment
- `apps/core-daemon/batch_orchestrator.py` — Parallel shade swarms with concurrency control
- `apps/core-daemon/model_registry.py` — Multi-tier model routing (auto/fixed/same)
- `apps/core-daemon/shade_stats.py` — Token usage tracking per shade/agent/model
- `apps/core-daemon/task_summarizer.py` — Intelligent fact-based summaries
- `apps/core-daemon/task_ledger.py` — Unified task history view
- Shade scope enforcement in `tools/__init__.py`
- Conversation search via FTS5 in `tools/search_tool.py`
- Tools: Http, Git, SpawnBatch, Search, UserModel, ProjectKnowledge
- Dynamic tool loader in `tools/dynamic_loader.py`
- Steering + follow-up queues in conversation engine
- Recurring task support with `not_before` scheduling
- Background worker thread (consolidation, goal inference, queue processing)
- Agent mode tracking (interactive/autonomous/delegating/idle)
- **Total: 374 tests passing**

### Remaining
- Soft specialization (task classification, role-aware compaction)
- Per-agent provider config
- Contract templates
- Overseer agent role
- Timed work sessions
- Remote agent linking

---

## 10. Risk Register

| Risk | Mitigation |
|------|-----------|
| OpenTUI is immature/underdocumented | Fallback: build on blessed/ink (Node TUI) or keep Textual as backup |
| Remote SSH tunnels are fragile | Implement aggressive reconnect + polling fallback + clear error display |
| LLM provider costs during shade execution | Hard budget limits per contract, token counting before API calls |
| Scope creep from 53 feature cards | This plan is the scope. Features not in phases 1-7 are post-launch. |
| Single-file JSON corruption under load | Phase 1.1 (SQLite) eliminates this immediately |
