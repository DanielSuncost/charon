# Charon Agent System Prompt, Memory & Specialization Design

> Design document for Charon's system prompt, memory architecture, compaction
> strategy, soft specialization, and shade/skill relationship.
> 
> Created: 2026-03-20
> Status: Active design discussion

---

## 1. Memory Representation: Text vs Embeddings vs Graphs

### What production systems actually use

| System | Memory Tech | How It Works |
|--------|------------|--------------|
| **Hermes (MEMORY.md)** | Plain text, `§`-delimited entries | 2200 char budget, agent curates via `memory` tool, injected frozen into system prompt at session start |
| **Hermes (session_search)** | SQLite FTS5 + LLM summarization | Keyword search over past sessions, top-3 results sent to Gemini Flash for focused summaries |
| **Hermes (Honcho)** | External AI-native memory service | Peer cards (structured facts), semantic search, LLM-synthesized recall, dialectic Q&A |
| **Pi** | None (session files only) | JSONL session persistence, LLM-generated compaction summaries. No cross-session memory. |
| **Claude Code** | `CLAUDE.md` context file | User-maintained static file. No agent-written memory. |
| **Cursor** | `.cursorrules` + embeddings index | Static rules + codebase embedding index for retrieval |
| **Mem0** | Vector store + graph | Embedding-based retrieval from vector DB, optional knowledge graph layer |
| **Cognee** | Knowledge graph + embeddings | Builds a graph from documents, uses graph traversal + embedding similarity for recall |

### Analysis: What actually matters

**Vector/embedding memory** solves one problem well: "find semantically similar past context." But it has real costs:
- Requires an embedding model running (local or API)
- Embedding quality varies wildly by model
- Retrieved chunks lack structure — you get fragments, not coherent context
- Hard to edit, inspect, or debug ("why did it remember X but not Y?")
- Overkill when the memory corpus is small (< 100 entries)

**Graph memory** solves a different problem: "understand relationships between entities." Useful for:
- "What files does module X depend on?"
- "Who worked on feature Y last?"
- "What decisions led to architecture Z?"
But also costly: requires entity extraction, relationship typing, graph maintenance. Fragile when entities are ambiguous.

**Plain text memory** (Hermes' approach) works because:
- Human-readable and editable — you can `cat MEMORY.md` and understand it
- LLM-native — models are good at reading and writing text
- Bounded and curated — the agent decides what's worth remembering
- No infrastructure — no embedding model, no vector DB, no graph database
- Debuggable — when the agent acts on stale memory, you can see exactly why

**SQLite FTS5** (Hermes' session_search) is the pragmatic middle ground for search:
- Full-text keyword search over past sessions
- No embedding model needed
- Fast, built into SQLite (which we already use)
- Combined with LLM summarization of matched sessions = good recall

### Recommendation for Charon

**Start with text + FTS5. Add embeddings later only if retrieval quality proves insufficient.**

Rationale:
- Charon's memory corpus per agent will be small initially (working memory = last 20 task summaries)
- The multi-agent coordination data (boundary proposals, contract results) is structured, not requiring semantic search
- FTS5 gives us keyword search for free since we're already on SQLite
- Text memory is inspectable — critical for a multi-agent system where you need to debug why Agent A made a certain decision
- Embeddings can be layered on later as an acceleration index over the text store, not a replacement for it

**Future gate for adding embeddings:** When an agent's accumulated knowledge exceeds ~50 entries and keyword search starts missing semantically relevant but lexically different matches, add an embedding column to the SQLite tables and use cosine similarity as a re-ranking signal on top of FTS5 results.

---

## 2. Compaction Strategy: Specialization-Aware

### The insight: different agent roles need different compaction

**Coordination/PM agents** (managing projects, tracking goals, assigning work):
- Need to remember: decisions made, who's working on what, blocked items, timeline
- Benefit from: Hermes-style **iterative update** — merge new information into a running summary that grows and evolves
- Don't need: file-level operation tracking, code snippets
- Compaction should preserve: goal hierarchy, assignment history, decision rationale

**Coding/implementation agents** (writing code, fixing bugs, running tests):
- Need to remember: what files they changed, what approaches they tried, what failed
- Benefit from: Pi-style **structured summary** (Goal, Progress/Done/InProgress, Key Decisions, Next Steps, Critical Context)
- Must preserve: file paths, function names, error messages, test results
- Compaction should include: file operation lists (read/modified)

**Research/analysis agents** (exploring codebases, reading docs, investigating issues):
- Need to remember: what they found, where they found it, what conclusions they drew
- Benefit from: structured findings format (Question, Sources Examined, Findings, Conclusions, Open Questions)
- Must preserve: file paths, relevant code snippets, URLs

### Implementation plan

The compaction prompt template is selected based on the agent's `role` + learned specialization:

```
COMPACTION_TEMPLATES = {
    'coordinator': ITERATIVE_UPDATE_TEMPLATE,   # Hermes-style merge
    'implementer': STRUCTURED_SUMMARY_TEMPLATE, # Pi-style structured
    'researcher': FINDINGS_SUMMARY_TEMPLATE,    # Research-focused
    'generalist': STRUCTURED_SUMMARY_TEMPLATE,  # Default to Pi-style
}
```

All templates share the iterative update capability (merge with previous summary when one exists). The difference is in what structure they impose on the summary.

---

## 3. Soft Specialization: How Agents Learn Their Role

### Concept

Every Charon agent starts as a **generalist**. As it works, it accumulates evidence about what it does well. After enough signal, it proposes a specialization to the user. The user confirms, and the specialization becomes visible in the UI.

### How it works

1. **Task classification** — every completed task is classified by type:
   - `code_change` — file edits, new files, test fixes
   - `coordination` — delegating to shades, boundary negotiation, goal management
   - `research` — reading files, exploring codebases, no writes
   - `infrastructure` — shell commands, build/deploy, env setup
   - `communication` — user interaction, status reports

2. **Affinity accumulation** — agent tracks a rolling window of last 30 task types. When one category exceeds 60%, the agent has a clear affinity.

3. **Proposal** — agent suggests specialization:
   > "Based on my recent work, I seem to be primarily doing coordination and project management. Want me to specialize as a **coordinator**? This will optimize my memory and compaction for tracking goals, assignments, and decisions."

4. **User confirmation** — user confirms or overrides. The role is stored on the agent record.

5. **Visibility** — the specialization appears:
   - Under the chat input box: `charon-project-01 · coordinator · running`
   - In the dashboard agent list: with a role badge
   - In the status bar

### What specialization changes

| Aspect | Generalist | Coordinator | Implementer | Researcher |
|--------|-----------|-------------|-------------|------------|
| Compaction template | Structured (Pi) | Iterative (Hermes) | Structured (Pi) | Findings-focused |
| System prompt focus | Balanced | Goals, assignments, decisions | Code, files, tests | Sources, findings, questions |
| Memory curation priority | Recent tasks | Decisions + assignments | File changes + errors | Discoveries + sources |
| Default shade strategy | Sequential phases | Parallel workers | Sequential phases | Fan-out search |

### What specialization does NOT change

- Tool access (all agents keep all tools)
- Authority level (all persistent agents are equal)
- User interaction style (always direct and helpful)

---

## 4. System Prompt Architecture for Charon

### Layers (in order)

1. **Identity block** — who this agent is
   ```
   You are {agent_name}, a persistent Charon agent.
   Role: {specialization or 'generalist'}
   Agent ID: {agent_id}
   Project: {project_name} ({project_path})
   Goal: {current_goal}
   ```

2. **Coordination awareness** — what other agents exist
   ```
   # Active Agents
   - charon-frontend-01 (implementer) — working on React components
   - charon-api-01 (implementer) — working on API routes
   [You are charon-frontend-01]
   
   # Pending Coordination
   - Boundary proposal from charon-api-01: scope overlap on src/shared/
   ```

3. **Working memory** — what this agent remembers (frozen snapshot)
   ```
   # Working Memory
   - [Mar 20 14:23] Refactored auth module, moved shared types to src/types/
   - [Mar 20 15:01] Tests passing after fixing import paths
   - [Mar 20 15:45] User prefers explicit error handling over try/catch
   ```

4. **Goal context** — from goal_runtime context packet
   ```
   # Current Goals
   - Active: "Implement OAuth2 flow for GitHub login" (3 tasks linked)
   - Blocked: "Add rate limiting" (waiting for API schema)
   ```

5. **Tools list + guidelines** — same as current (pi-style)

6. **Context files** — `AGENTS.md`, `CLAUDE.md`, `CHARON.md` from project directory. Scanned for injection (Hermes-style).

7. **Shade context** (only for shade agents) — contract constraints
   ```
   # Shade Contract
   Contract: ctr-abc123
   Phase: P02 (implementation)
   Parent agent: charon-project-01
   Objective: Implement the login form component
   Constraints:
   - Only modify files in src/components/auth/
   - Do not change any API routes
   - Maximum 3 new files
   Expected outputs:
   - LoginForm.tsx component
   - Unit tests passing
   Budget: 30,000 tokens / 5 minutes
   ```

8. **Date + CWD** — always last

### Key design decisions

- **Frozen snapshot**: like Hermes, memory is frozen at task start. Preserves prefix cache.
- **Coordination context is lightweight**: just names, roles, and pending items. Not full agent state.
- **Shade constraints are hard rules**: injected into the prompt so the model knows its boundaries. Violations are caught by scope-checking tool middleware.
- **No ephemeral prompts**: unlike Hermes, we don't have a gateway/platform layer. All context goes in the system prompt.

---

## 5. Shades vs Skills: How Shades Handle What Skills Do

### What skills are in Pi/Hermes

A **skill** in Pi/Hermes is a markdown file with instructions for a specific task type. The model reads it on demand when it recognizes a matching task. Examples:
- "How to set up a Python project with uv"
- "How to write a React component with proper testing"
- "How to debug a Docker container"

Skills are **passive knowledge** — a recipe the agent reads and follows.

### How Charon handles this differently

Charon doesn't have skills files. Instead, the **shade contract** serves the same purpose but as an **active execution unit**:

1. **Pi/Hermes skill flow:**
   ```
   User asks "set up a Python project"
   → Agent scans skill index
   → Agent reads python-setup/SKILL.md
   → Agent follows the instructions in the skill
   → One agent does everything sequentially
   ```

2. **Charon shade flow:**
   ```
   User asks "set up a Python project"
   → Persistent agent decomposes the task
   → Creates shade contract with phases:
     P01: Analyze requirements (what kind of project, dependencies)
     P02: Scaffold project structure (pyproject.toml, src/, tests/)
     P03: Configure tooling (ruff, mypy, pytest)
     P04: Verify (run tests, check linting)
   → Each phase gets a shade with:
     - Phase-specific objective (= the "skill" instructions)
     - Constraints (don't touch files outside scope)
     - Budget (token/time limit)
   → Shades execute, possibly in parallel
   → Parent agent collects results, reports to user
   ```

### What this means in practice

- **Simple tasks** (< 220 chars, narrow scope) — the persistent agent handles them directly, no shades. This is equivalent to a Pi agent following a simple skill.
- **Complex tasks** (multi-step, broad scope) — shades are spawned. The shade contract's phase objectives ARE the skill instructions, but decomposed and parallelizable.
- **Reusable patterns** — if Charon notices it keeps creating similar shade contracts (e.g., "set up Python project"), it can save the phase pattern as a **contract template** that it reuses next time. This is the shade equivalent of a skill.

### Contract templates (future)

```json
{
  "name": "python-project-setup",
  "description": "Set up a new Python project with modern tooling",
  "phases": [
    {"name": "analyze", "objective": "Determine project type, dependencies, Python version"},
    {"name": "scaffold", "objective": "Create pyproject.toml, src/ layout, tests/ structure"},
    {"name": "configure", "objective": "Set up ruff, mypy, pytest, pre-commit"},
    {"name": "verify", "objective": "Run all tools, ensure clean output"}
  ],
  "constraints": ["Use uv for dependency management", "Target Python 3.12+"],
  "learned_from_tasks": ["task-abc123", "task-def456"]
}
```

The persistent agent builds these templates from experience (soft specialization learning).

### Key difference from skills

| Aspect | Pi/Hermes Skills | Charon Shades |
|--------|-----------------|---------------|
| Execution | Single agent reads and follows | Multiple shades execute phases |
| Parallelism | None (sequential) | Phases can run in parallel |
| Budget | None (agent uses full context) | Per-phase token/time limits |
| Scope | Agent has full access | Each shade scoped to contract |
| Model | Same model for everything | Different models per phase |
| Learning | Static files, user-maintained | Contract templates learned from experience |
| Failure | Agent retries or gives up | Branch-from-failure, re-plan specific phase |

---

## 6. Model Routing for Shades

### The problem

Not every phase needs the strongest model. Analysis and verification phases can use fast/cheap models. Implementation phases need strong models. Currently all shades use the same globally-configured model.

### Model tiers

| Tier | Use case | Examples | Cost |
|------|----------|---------|------|
| **Fast** | Analysis, verification, summarization | qwen3-8b, gemini-flash, gpt-4o-mini | Low |
| **Strong** | Implementation, complex reasoning | qwen3-30b, claude-sonnet, gpt-4o | Medium |
| **Specialist** | Deep reasoning, architecture | claude-opus, o3 | High |

### Phase-to-model mapping

```python
PHASE_MODEL_MAP = {
    'analysis': 'fast',
    'planning': 'strong',
    'implementation': 'strong',
    'verification': 'fast',
    'report': 'fast',
    'research': 'fast',
    'refactoring': 'strong',
    'debugging': 'strong',
}
```

The persistent agent can override this per-contract based on task complexity.

### Model registry

Each Charon instance maintains a registry of available models:

```python
{
    "fast": {"provider": "local", "model_id": "qwen3-8b", "context_window": 32768},
    "strong": {"provider": "local", "model_id": "qwen3-30b-a3b", "context_window": 65536},
    "specialist": {"provider": "anthropic", "model_id": "claude-sonnet-4", "context_window": 200000},
}
```

If only one model is configured, all tiers map to it. No complexity when running single-model.

---

## 7. Future: Voice Integration Notes

(Deferred — recording the design intent for later)

- **Wake word** — local detection (Porcupine/OpenWakeWord), always-listening, low CPU
- **STT** — Whisper (local) or Deepgram (API), triggered by wake word
- **TTS** — Piper/Kokoro (local) or ElevenLabs (API), reads responses
- **Interruption** — "stop" / "cancel" detected by wake word engine, triggers `engine.abort()`
- **Integration point** — voice input becomes a `user_intent` task, same as typed input. Voice output is a post-processing step on chat responses.
- **UI** — microphone indicator in status bar, voice activity visualization

---

## 8. Implementation Priority

1. **System prompt builder** — implement the layered system prompt from Section 4
2. **Memory refresh per task** — ensure context is fresh, not stale from engine cache
3. **Context file discovery** — `AGENTS.md`/`CLAUDE.md`/`CHARON.md` with injection scanning
4. **Compaction with role-aware templates** — Pi structured + Hermes iterative, selected by specialization
5. **Soft specialization tracking** — task classification + affinity accumulation
6. **Model registry + routing** — model tiers, phase-to-model mapping
7. **Contract templates** — learned shade patterns saved for reuse
8. **FTS5 session search** — keyword search over past task history
9. **Voice** — wake word + STT + TTS pipeline

---

## Appendix: Comparison Summary

### Pi Agent
- **Identity**: Generic "expert coding assistant"
- **Memory**: None. Session files only. `AGENTS.md`/`CLAUDE.md` for static context.
- **Compaction**: LLM-generated structured summary (Goal/Progress/Decisions/Next Steps). Iterative update merges with previous summary. Tracks file operations. ~20K recent tokens kept.
- **Skills**: Markdown files with frontmatter, indexed in system prompt as XML, model reads on demand.
- **Multi-agent**: None.
- **Injection defense**: None.

### Hermes Agent
- **Identity**: Configurable name via Honcho. "Hermes Agent, an intelligent AI assistant created by Nous Research."
- **Memory**: Three layers — MEMORY.md (agent notes, 2200 chars), USER.md (user profile, 1375 chars), Honcho (external AI memory with semantic search + dialectic Q&A). All frozen at session start.
- **Compaction**: Trajectory compression for training data. Live sessions rely on context window + Honcho for cross-session continuity.
- **Skills**: SKILL.md files in ~/.hermes/skills/, categorized, with conditional activation (fallback_for/requires). Auto-save approach after complex tasks.
- **Session search**: FTS5 keyword search over SQLite session DB, top-3 matched sessions summarized by Gemini Flash.
- **Multi-agent**: None (single agent, multi-platform via gateway).
- **Injection defense**: Content scanning for MEMORY.md writes and context file loading. Blocks prompt injection patterns and invisible unicode.
- **Platform awareness**: WhatsApp/Telegram/Discord/Slack/Signal/Email/CLI-specific formatting.

### Charon Agent (this design)
- **Identity**: Per-agent name, role (specialization), goal, project. Shade agents get contract context.
- **Memory**: Per-agent working memory (text, curated), per-project knowledge (shared), global user preferences. Frozen snapshot at task start. FTS5 for search.
- **Compaction**: Role-aware templates — iterative update for coordinators, structured summary for implementers. All support incremental merge with previous summary.
- **Skills**: Replaced by shade contracts with phase-specific objectives. Contract templates learned from experience.
- **Multi-agent**: Core feature. Coordination context in system prompt. Boundary negotiation. Shade orchestration with parallel execution.
- **Injection defense**: Hermes-style content scanning for context files.
- **Model routing**: Per-phase model selection. Fast models for analysis/verification, strong models for implementation.
- **Soft specialization**: Agents learn their role from task patterns, propose specialization to user, role affects compaction/prompting/shade strategy.
