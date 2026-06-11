# Charon Capability Roadmap

> Unified, prioritized list of capabilities to build.
> Consolidated from gap analysis across Hermes, Pi, OpenClaw, and OpenCode.
> Updated: 2026-06-11

---

## P0 — Core Capability Gaps

These close real functional gaps that limit what Charon can do today.

### Memory & Recall
- [ ] **Hybrid session recall** — FTS + semantic + LLM summarization of retrieved episodes (not just raw snippets)
- [ ] **Memory quality controls** — deduplication, contradiction detection, stale-entry pruning
- [ ] **Memory inspection UX** — inspect, accept, reject, prune individual entries
- [ ] **Cross-agent memory consolidation** — repeated discoveries become canonical project facts
- [ ] **Episode clustering and filtering** — group sessions by task/theme, filter by project/agent/time/outcome

### Browser & Web
- [x] **Browser tool** — Playwright-based navigation, click, type, scroll
- [ ] **DOM/accessibility-tree browsing** — structured page access, not just screenshots
- [ ] **Browser vision** — screenshot capture + vision model analysis
- [ ] **Persistent browser sessions** — maintain cookies/state across steps
- [ ] **Website policy controls** — domain restrictions for browser access

### Checkpoints & Rollback
- [ ] **Automatic checkpoints before mutations** — transparent shadow git snapshots
- [ ] **Checkpoint metadata** — link agent, task, goal, timestamp to each snapshot
- [ ] **Easy restore UX** — one-command rollback to previous checkpoint
- [ ] **Checkpoint diff inspection** — view what changed before deciding to rollback
- [ ] **Checkpoint integration with shades and judge loops**

### MCP (Model Context Protocol)
- [ ] **MCP client runtime** — connect to MCP servers, discover tools
- [ ] **Dynamic tool discovery** — auto-list available MCP tools
- [ ] **Namespace isolation** — prevent tool name collisions across servers
- [ ] **Per-agent/per-project MCP enablement** — granular scope control
- [ ] **MCP auth and policy** — credential management, approval for untrusted tools

### Provider Resilience
- [ ] **Provider fallback** — automatic retry with alternative provider on failure
- [ ] **Exponential backoff** — retry logic for transient failures
- [ ] **Health-aware routing** — select provider based on current availability
- [ ] **Graceful degradation** — clear messaging in reduced-capability mode

### Safety & Approval
- [x] **Tool approval flow** — user confirms destructive actions
- [ ] **Risk-aware command classification** — categorize by risk level (safe/medium/destructive)
- [ ] **Secret access controls** — prevent accidental credential exposure
- [ ] **Network policy controls** — restrict which endpoints agents can reach
- [ ] **Audit trails** — log significant operations for debugging

---

## P1 — Deepen Native Strengths

These extend what makes Charon unique: multi-agent coordination, shades, and the TUI.

### Shade Contracts
- [ ] **Contract templates** — predefined structures per task type with acceptance criteria
- [ ] **Shade partial failure handling** — graceful recovery from partial completion
- [ ] **Phase resume/retry** — retry a failed phase without restarting the whole task
- [ ] **Better parent-child memory handoff** — improved context passing to/from shades
- [ ] **Learned contract templates** — auto-capture successful shade patterns as reusable templates

### Shade Observability
- [ ] **Phase status** — real-time visibility into current shade execution phase
- [ ] **Cost and token tracking** — per-shade consumption metrics
- [ ] **Model routing** — track which models/providers each shade used
- [ ] **Output inspection** — clear interface to review shade artifacts

### Multi-Agent Coordination
- [ ] **Shared task board** — central workspace where agents see and claim tasks
- [ ] **File/subsystem ownership leases** — prevent conflicting edits
- [ ] **Conflict detection** — alerts when agents modify same files
- [ ] **Inter-agent request/handoff protocols** — structured task passing between agents
- [ ] **Dependency tracking** — graph of cross-agent task dependencies
- [ ] **Situational awareness packet** — summary of other agents' work injected into prompts

### Project Knowledge
- [ ] **Conventions surfaced before edits** — prompt agents with relevant standards
- [ ] **Build/test/run recipes** — structured project commands as capability info
- [ ] **Architecture maps** — subsystem structure representations
- [ ] **Known gotchas** — warn before risky operations
- [ ] **Onboarding packet for new agents** — condensed context bundle

### TUI Polish
- [ ] **Search and recall panels** — side panel for session search within TUI
- [ ] **Inbox/coordination panels** — visible inter-agent message queue
- [ ] **Live diff panels** — side-by-side file comparison
- [ ] **Checkpoint browser** — interactive viewer to browse/compare/restore
- [ ] **Intervention timeline** — visual history of human steering events
- [ ] **Stronger keyboard workflows** — comprehensive keybindings for rapid ops

### Compaction Quality
- [x] **File-aware compaction** — track files read/edited in compaction summary
- [x] **Memory flush before compaction** — save discoveries to long-term memory first
- [x] **Tool pair sanitization** — fix orphaned tool_call/result after compaction
- [ ] **Goal-aware summaries** — bias compaction toward current goal context
- [ ] **Artifact-aware summaries** — preserve references to key outputs
- [ ] **Compaction quality checks** — automated tests for information retention

---

## P2 — Workflow Superiority

### Procedures & Automation
- [ ] **Reusable procedures** — library of multi-step procedures with arguments and discovery
- [ ] **Procedure versioning** — review changes before deployment
- [ ] **Project-local and global scoping** — procedures scoped to project or global
- [ ] **Learning from repeated workflows** — auto-capture and refine patterns
- [ ] **Stronger recurring automations** — more reliable scheduled tasks
- [ ] **Automation approval gates** — user sign-off before destructive changes
- [ ] **Automation observability** — retry logic, logging, metrics

### Integrations
- [ ] **GitHub/forge integration** — issue/PR/review workflows
- [ ] **Notification surfaces** — configurable alerts from Charon activities
- [ ] **Soft specialization** — task classification, role-aware compaction
- [ ] **Overseer agent role** — pre-built oversight specialization
- [ ] **Timed work sessions** — time-boxed work with automatic summaries
- [ ] **Voice integration** — voice input/output

---

## P3 — Strategic: Multi-System Interoperability

- [ ] **Deep agent bridge plugins** — structured APIs for Hermes, Pi, Claude Code, Codex, OpenCode
- [x] **Structured task dispatch (Harbor)** — dispatch tasks to remote agents over SSH with voyage manifests
- [x] **Result normalization** — structured results with memory ingestion from remote workers
- [x] **Capability maps per agent type** — `/harvest_souls` scans peer repos and catalogs abilities
- [ ] **Memory bridging** — convert external agent work into Charon's memory model
- [ ] **Mixed-agent search** — unified search across all agent systems
- [ ] **Cross-agent provenance** — track which system performed each piece of work
- [ ] **Fleet-level policy controls** — security policies across all agents

---

## Completed

Items marked [x] above, plus:
- [x] Shade constraint enforcement
- [x] Conversation search (FTS5)
- [x] Unified chat + daemon
- [x] Web search/extraction tool
- [x] Multi-provider support with mid-session switching
- [x] Dynamic tool loader
- [x] Agent coordination with boundary detection
- [x] SQLite persistence with conversation search and resume
- [x] Harbor protocol — remote task dispatch with mid-task recall and memory ingestion
- [x] `/harvest_souls` — scan peer agent repos, LLM-ranked gap analysis, interactive adoption workflow
- [x] Shared clipboard module — platform-aware (pbcopy/OSC52), tmux passthrough
- [x] Alternate screen buffer — proper mouse capture in terminal emulators
- [x] Auth error resilience — dismiss auth dialog on failure, reuse existing OAuth tokens
- [x] Conversation rooms — multi-agent structured discussions with turn orchestration
