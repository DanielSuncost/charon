# Feature Delta Analysis: Charon vs Pi / Hermes / Openclaw / OpenCode

> What each system has that we don't, what we have that they don't,
> and what it means for priorities.
> Updated: 2026-03-21

---

## What they have that we don't

### Pi-agent

| Feature | Pi has | Charon has | Gap |
|---------|--------|-----------|-----|
| **grep/find/ls tools** | Dedicated tools (faster, respects .gitignore) | Bash only | Low — Bash works fine |
| **Extensions** | TypeScript plugin system with lifecycle hooks | Dynamic tool loader (Python) | Different approach, ours is simpler |
| **Prompt templates** | Reusable prompt snippets with frontmatter | Not built | Medium — useful for repeated patterns |
| **Themes** | Full theme system with colors/styles | Hardcoded colors | Low — cosmetic |
| **Session branching UI** | Visual branch/switch in TUI | Intervention graph (backend only) | Medium — data exists, UI doesn't expose it |
| **Compaction with file tracking** | Tracks read/edited files in compaction summary | Generic compaction | Medium — would improve context quality |
| **beforeToolCall/afterToolCall hooks** | Extensions can intercept tool calls | Scope enforcement only | Low — scope covers the safety case |
| **Package manager** | npm install/remove for extensions | Not applicable | N/A |
| **Scoped models (Ctrl+P cycling)** | Cycle through configured model list | /provider switch | Low — different UX, same capability |

### Hermes

| Feature | Hermes has | Charon has | Gap |
|---------|-----------|-----------|-----|
| **Web search** | Firecrawl + LLM summarization | Http tool (raw fetch only) | **High** — agents can't research |
| **Browser** | agent-browser with accessibility tree | Not built | **High** — can't verify web apps |
| **Session search** | FTS5 + LLM summarization of matches | FTS5 (raw snippets) | Medium — we return snippets, they return summaries |
| **Todo tool** | Structured task list the agent manages | Goal system (richer) | Low — our goals are more capable |
| **Vision tools** | Image analysis | Not built | Medium — depends on model |
| **TTS/transcription** | Voice input/output | Not built (planned) | Deferred |
| **MCP support** | Model Context Protocol tools | Not built | Medium — emerging standard |
| **Checkpoint manager** | Shadow git repos, transparent snapshots | Git tool (explicit) | Medium — theirs is transparent |
| **Mixture of agents** | Multiple models collaborate on one response | Shade swarms (separate tasks) | Low — different pattern |
| **Platform awareness** | WhatsApp/Telegram/Discord formatting | CLI only | N/A for us |
| **Injection scanning** | Memory + context file scanning | ✅ We have this | Parity |

### Openclaw

| Feature | Openclaw has | Charon has | Gap |
|---------|-------------|-----------|-----|
| **Subagent system** | Full subagent spawning, lifecycle, depth limits | Shade swarms + SpawnBatch | Comparable |
| **Sandbox/Docker** | Isolated execution in containers | No sandboxing | **High** — safety for untrusted code |
| **Tool approval flow** | User confirms dangerous tool calls | Shade scope enforcement | Medium — we enforce scope, not approval |
| **Auth profiles** | Multiple auth configs, rotation, failover | Single provider + shade provider | Medium |
| **MCP support** | Full MCP integration | Not built | Medium |
| **Daemon/service** | launchd/systemd service management | Manual daemon | Low — convenience |
| **Canvas/web UI** | Web-based collaborative UI | Terminal TUI only | Different approach |
| **Cron scheduling** | Native cron job support | Recurring tasks (heartbeat-based) | Low — ours works |
| **Model fallback** | Automatic failover between models | Not built | Medium |

### OpenCode

| Feature | OpenCode has | Charon has | Gap |
|---------|-------------|-----------|-----|
| **Multi-provider config** | YAML config with multiple providers/models | onboarding.json + model_registry | Comparable |
| **LSP integration** | Language server protocol | Not built | Medium — would improve code intelligence |
| **Diff view** | Side-by-side diff display | Git diff output | Low — cosmetic |

---

## What we have that they ALL lack

| Charon feature | Pi | Hermes | Openclaw | OpenCode |
|----------------|-----|--------|----------|----------|
| **Three-tier memory** (user/project/agent) | ❌ | Partial (MEMORY.md) | ❌ | ❌ |
| **Background consolidation** (LLM updates user model) | ❌ | ❌ | ❌ | ❌ |
| **Parallel shade swarms** (SpawnBatch) | ❌ | ❌ | Partial (subagents) | ❌ |
| **Autonomous goal-driven work** | ❌ | ❌ | ❌ | ❌ |
| **Goal inference from conversation** | ❌ | ❌ | ❌ | ❌ |
| **Shade scope enforcement** (tool-call level) | ❌ | ❌ | ✅ (path policy) | ❌ |
| **Per-task model routing** (complexity→tier) | ❌ | ❌ | ❌ | ❌ |
| **Agent mode tracking** (interactive/autonomous/delegating) | ❌ | ❌ | ❌ | ❌ |
| **Idea capture + goal backlog** | ❌ | ❌ | ❌ | ❌ |
| **Shade usage stats** (tokens per shade/model) | ❌ | ❌ | ❌ | ❌ |
| **Dynamic tool loader** (agent builds own tools) | ❌ | ❌ | ❌ | ❌ |
| **Steering + follow-up queues** | ✅ (pi has this) | ❌ | ❌ | ❌ |
| **Structured user model** (7 categories) | ❌ | Partial (USER.md) | ❌ | ❌ |
| **Intelligent task summarization** (fact-based) | ❌ | ❌ | ❌ | ❌ |

---

## Priority assessment

### Must address (real capability gaps)

**1. Web search/extraction.** Both Hermes and Openclaw have this. Our
agent can't look anything up on the internet. A lightweight
implementation: httpx fetch + html-to-text + optional LLM summarization.
No Firecrawl dependency needed.

**2. Compaction with file tracking.** Pi tracks which files were read
and modified during compaction. This is significantly better context
than our generic "summarize the conversation." The information is
already available from tool calls — we just need to extract it during
compaction like pi does.

**3. Model fallback.** If the configured provider is down or rate-limited,
everything stops. Openclaw has automatic failover. We should at minimum
try the shade provider as fallback, or retry with exponential backoff.

### Should address (competitive features)

**4. MCP support.** Emerging standard. Both Hermes and Openclaw support
it. Lets the agent use any MCP server's tools. Would significantly
expand capabilities without building each tool ourselves.

**5. Session search with LLM summarization.** Hermes doesn't just
return FTS5 snippets — it summarizes matched sessions with a fast model.
Our Search tool returns raw excerpts. Adding summarization would make
recall much more useful.

**6. Tool approval flow.** Openclaw asks the user before running
dangerous commands. We enforce scope for shades but persistent agents
can do anything. An optional approval mode for destructive operations
(rm, chmod, etc.) would add safety.

**7. Transparent git checkpoints.** Hermes' checkpoint manager creates
shadow git repos and snapshots automatically before file mutations. Our
Git tool requires explicit use. Making checkpoints transparent would
enable "undo the last thing the agent did" for free.

### Low priority (nice to have)

**8. grep/find/ls tools.** Marginal improvement over Bash.

**9. Prompt templates.** Useful but our system prompt builder + dynamic
tools cover most of the use case.

**10. LSP integration.** Would improve code intelligence but adds
significant complexity.

**11. Vision tools.** Depends on model support. Nice for screenshot
analysis but not core workflow.

---

## Updated feature plan (ordered)

1. ~~Shade constraint enforcement~~ ✅ Done
2. ~~Conversation search~~ ✅ Done  
3. ~~Unified chat+daemon~~ ✅ Done
4. ~~Web search/extraction tool~~ ✅ Done
5. ~~Compaction with file tracking~~ ✅ Done
6. ~~Tool approval flow~~ ✅ Done
7. ~~Browser tool~~ ✅ Done (Playwright + browser-use)
8. **Memory flush before compaction** — save knowledge before context loss
9. **Tool pair sanitization** — fix orphaned tool_call/result after compaction
10. **Model fallback** — retry with shade provider, exponential backoff
11. **Soft specialization** — task classification, role-aware compaction
12. **MCP support** — connect to MCP servers
13. **Agent bridge plugins** — structured API communication with pi (RPC), hermes (gateway), openclaw (daemon). Beyond tmux screen bridging — dispatch tasks, collect results, use them as specialized workers.
14. **Session search summarization** — LLM distill FTS5 results
15. **Transparent checkpoints** — auto-snapshot before mutations
16. **Per-agent provider config** — pair programming
17. **Contract templates** — learned shade patterns
18. Overseer agent role
19. Timed work sessions
20. Voice integration
21. Remote agent linking
