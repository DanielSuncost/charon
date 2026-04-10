# Charon Autonomous Implementation Master Plan

> Purpose: a concise, implementation-ready plan for autonomous teams to execute safely without breaking existing Charon strengths.
>
> Scope: Phase 1 only. This plan intentionally limits scope to the highest-priority capability gaps that must be closed before broader autonomy work.

Updated: 2026-04-05
Status: Ready for implementation

---

## 1. Objective

Make Charon competitive with Hermes on core single-agent capability **without regressing** Charon’s existing strengths:

- persistent named agents
- three-tier memory
- shared project knowledge
- session grid / TUI
- shades
- Libris
- judge loops
- Charon’s Boat

This phase is complete when Charon no longer loses obvious comparisons on:
- session recall
- browser/web tasks
- checkpoints/undo
- MCP support
- reliability/fallback
- approval/safety

---

## 2. Hard constraints

Autonomous teams must obey these constraints:

### 2.1 Do not regress core architecture
Do not break or weaken:
- `UserModel`
- `ProjectKnowledge`
- agent working memory
- shade orchestration
- judge loops
- Rust TUI session grid
- Charon’s Boat compatibility

### 2.2 Prefer additive integration
New systems must be additive and gated, not invasive rewrites.
Use feature flags or config gates where appropriate.

### 2.3 No broad refactors unless required
Do not perform repo-wide cleanup or unrelated architectural rewrites during this phase.
If a refactor is necessary, keep it local to the workstream.

### 2.4 Every workstream must ship with validation
Each implementation must include:
- acceptance tests
- failure-path behavior
- rollback or disable path
- docs for operation and limits

### 2.5 Safety before autonomy
If a change increases power, it must also improve at least one of:
- rollback
- observability
- approval control
- runtime resilience

---

## 3. Delivery model

Execute as six parallel-but-coordinated workstreams.

Recommended order of merge:
1. transparent checkpoints
2. reliability / fallback
3. approval / safety
4. session recall
5. browser / web
6. MCP support

Reason: first improve recoverability and trust, then add capability.

---

## 4. Workstream specs

## W1 — Transparent checkpoints and undo

### Goal
Make code/file mutations safe by default and easy to reverse.

### Deliverables
- automatic checkpoint creation before file-mutating operations
- checkpoint metadata including:
  - agent id/name
  - task/goal summary
  - timestamp
  - working directory
- restore command/path
- diff inspection path
- “undo last agent action” path
- integration points for shades and judge loops

### Implementation rules
- use shadow-git style checkpointing or equivalent isolated mechanism
- do not leak git state into user repos
- checkpoint creation must be cheap enough for routine use
- large-repo edge cases must fail safe, not corrupt state

### Required tests
- checkpoint created before write/edit mutation
- restore returns working tree to pre-mutation state
- diff inspection works on created checkpoints
- repeated operations in one task do not create broken metadata chains
- checkpoint system does not alter user repo git config/state

### Acceptance criterion
A user can ask Charon to edit code, inspect what changed, and undo it immediately and safely.

---

## W2 — Runtime reliability and model fallback

### Goal
Reduce brittle failures and preserve task continuity under provider/model issues.

### Deliverables
- provider fallback policy
- retry/backoff policy
- failure classification for transient vs terminal errors
- degraded-mode behavior with clear messaging
- health-aware routing defaults

### Implementation rules
- preserve current successful provider behavior by default
- fallback must be explicit in logs/status
- do not silently switch behavior in ways users cannot inspect
- maintain compatibility with shade provider separation

### Required tests
- transient provider error retries correctly
- terminal provider error triggers fallback when configured
- fallback preserves task state and returns intelligible output
- disabled fallback mode preserves old behavior

### Acceptance criterion
A provider outage or rate-limit no longer causes routine task collapse when a viable fallback exists.

---

## W3 — Approval and safety controls

### Goal
Make dangerous actions controllable without crippling normal workflows.

### Deliverables
- risk classifier for tool actions
- approval gate for destructive/high-risk actions
- configurable approval modes by agent/project
- policy controls for:
  - shell/file mutation
  - network use
  - secret-sensitive paths/actions
- audit log of gated actions

### Implementation rules
- low-risk actions must remain low-friction
- approval UX must be clear and minimal
- persistent agents need safety controls, not just shades
- policies must degrade gracefully if unset

### Required tests
- destructive file/shell action is gated in approval mode
- low-risk actions are not unnecessarily blocked
- per-project policy overrides work
- audit records contain enough detail for review

### Acceptance criterion
Users can trust autonomous actions because risky behavior is visible and controllable.

---

## W4 — Session recall and summarized search

### Goal
Turn past work into reliably retrievable, concise, actionable memory.

### Deliverables
- hybrid retrieval combining:
  - FTS/keyword retrieval
  - semantic retrieval
- LLM summarization of retrieved episodes
- result schema including:
  - date/time
  - project
  - agent
  - key files
  - outcome
  - provenance
- filters by project / agent / time
- ranking tuned for practical prior-work recovery

### Implementation rules
- retrieval should return episodes, not transcript dumps
- provenance must be surfaced
- preserve current search functionality as a fallback path
- summarization must be bounded and cheap enough for routine use

### Required tests
- known prior-fix retrieval returns the correct episode
- retrieved summary includes important files/actions/outcome
- no-match path is clean and non-hallucinatory
- retrieval works across realistic multi-session history

### Acceptance criterion
“Find how we solved this last time” becomes a dependable workflow.

---

## W5 — Browser and web operations

### Goal
Enable reliable real-world web research and browser task execution.

### Deliverables
- persistent browser session abstraction
- page inspection via DOM/accessibility representation
- screenshot capture
- vision-assisted page analysis
- stable interaction primitives:
  - navigate
  - click
  - type
  - scroll
  - upload
  - download
- support for login and multi-step flows
- browser-safe policy controls
- browser activity summarized into task memory/compaction

### Implementation rules
- do not break existing Browser tool workflows
- browser sessions must clean up reliably
- local backend comes first; remote/cloud backend may be additive
- errors must expose enough state for debugging

### Required tests
- navigate + inspect page
- fill and submit multi-step form
- preserve session state across steps
- screenshot + vision analysis path works
- cleanup path does not leak stuck sessions

### Acceptance criterion
Charon can complete realistic browser workflows reliably enough for routine use.

---

## W6 — First-class MCP support

### Goal
Make Charon interoperable with the MCP ecosystem in a safe, usable way.

### Deliverables
- MCP client integration
- dynamic discovery of MCP tools
- namespaced registration / collision handling
- per-agent/per-project enablement
- auth/config path
- policy controls for MCP-originated tools

### Implementation rules
- MCP tools must not collide ambiguously with built-in tools
- disabled MCP state must preserve current behavior
- tool provenance should be inspectable
- remote MCP usage must respect approval/safety policy where relevant

### Required tests
- discover tools from configured MCP server
- invoke discovered tools successfully
- handle name collisions deterministically
- disable MCP per project or agent

### Acceptance criterion
A user can attach useful MCP servers and have Charon use them safely and clearly.

---

## 5. Cross-workstream interfaces

To avoid breakage, the following interfaces must remain stable unless explicitly versioned:

- system prompt assembly layers
- `UserModel` / `ProjectKnowledge` tool contracts
- shade orchestration contract model
- TUI session discovery and rendering behavior
- Charon’s Boat session registration behavior
- existing Browser tool public interface unless version-gated

If a workstream must alter one of these, it must:
1. document the interface change
2. preserve backward compatibility where feasible
3. include migration notes/tests

---

## 6. Integration policy

### Merge policy
Each workstream should land behind a stable integration boundary.

Recommended merge checkpoints:
- W1 before W3/W5
- W2 before or alongside W5/W6
- W3 before enabling higher-risk autonomous flows that consume W5/W6
- W4 can land independently
- W5 and W6 should consume W2/W3 policies instead of inventing their own

### Conflict policy
If workstreams overlap, prefer:
- shared policy modules
- shared telemetry/logging conventions
- additive tool registration
- minimal changes to prompt assembly

Do not duplicate:
- approval logic
- retry logic
- capability gating
- provenance/result schema concepts

---

## 7. Required benchmark exits

The plan is not done until these benchmarks pass:

### B1 — Prior-fix recall
Recover a prior solution with files, outcome, and provenance.

### B2 — Browser workflow
Complete a real multi-step browser task without losing state.

### B3 — Safe rollback
Make a change and undo it immediately via checkpoint restore.

### B4 — MCP interoperability
Connect to an MCP server, discover tools, and use them safely.

### B5 — Provider outage recovery
Survive a provider/model failure with retry/fallback behavior.

### B6 — Dangerous action gating
Trigger approval for a destructive action without blocking harmless work.

If these are not passing, this plan is not complete.

---

## 8. Minimal success definition

This phase succeeds if all six are true:

1. Charon can recover prior project work through concise summarized recall
2. Charon can complete realistic browser/web workflows reliably
3. Charon can safely roll back agent changes with low friction
4. Charon can use MCP tools in a first-class way
5. Charon can recover gracefully from provider failures
6. Charon has a credible approval/safety model for risky actions

---

## 9. Explicit non-goals for this implementation phase

Do not expand scope into:
- broad messaging-platform parity
- major TUI redesign unrelated to these workstreams
- procedure/skills overhaul
- new research-system features beyond necessary integration points
- deep bridge plugins for external agents

Those belong to later phases.

---

## 10. Tasking guidance for autonomous teams

Each autonomous team should receive exactly one workstream with:
- scope limited to its section above
- frozen interfaces listed in Section 5
- required tests from its workstream section
- benchmark exit(s) from Section 7

Each team must return:
1. implementation
2. tests
3. short design note
4. explicit statement of unchanged interfaces
5. known limitations

This keeps teams parallelizable and reduces cross-team breakage.
