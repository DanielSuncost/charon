# Charon vs Hermes Delta Matrix

> Companion to `docs/plans/charon-vs-hermes-superiority-plan.md`
>
> Purpose: show the current state, target state, leverage, and implementation priority for the major areas where Charon must either close a gap or extend a lead.

Updated: 2026-04-05

---

## Legend

### Status
- **Lead** — Charon is already meaningfully ahead
- **Competitive** — roughly comparable, but not decisively ahead
- **Gap** — Hermes appears stronger today
- **Unknown** — insufficient validation; benchmark required

### Priority
- **P0** — foundational / must address early
- **P1** — high-value differentiator
- **P2** — important, but can follow core foundations
- **P3** — later / strategic moat

### Difficulty
- **S** — small
- **M** — medium
- **L** — large
- **XL** — very large / multi-phase

---

## Matrix

| Area | Current Charon state | Hermes state | Delta status | Desired end state | Priority | Difficulty |
|---|---|---|---|---|---|---|
| **Three-tier shared memory** | Global user model + project knowledge + agent working memory | Strong memory, but centered on local memory files + external provider stack | **Lead** | Keep lead and improve trust/retrieval quality | P1 | M |
| **Session recall quality** | Search + recall primitives; not yet obviously best-in-class episode recovery UX | FTS5 session search with LLM summarization | **Gap** | Hybrid FTS + semantic + summarized recall with provenance | P0 | L |
| **Memory trust/provenance** | Structured tiers, shared state | Strong local memory stack, but less explicitly cross-agent shared | **Competitive** | Canonical facts, provenance, contradiction detection, review UX | P0 | L |
| **Cross-agent memory sharing** | Core design strength | Delegation/subagents exist, but shared memory topology is less central | **Lead** | Make shared memory operational and highly trustworthy | P1 | M |
| **Browser/web operations** | Browser tool exists, but must prove parity on real workflows | Mature browser stack, cloud/local, good test coverage | **Gap** | Reliable web app operation and web research at parity or better | P0 | XL |
| **Session search UX** | Useful, but not yet clearly superior | Strong summarized search workflow | **Gap** | Episode cards, clustering, filters, summaries, comparisons | P0 | L |
| **Transparent checkpoints** | Checkpoint support exists in architecture/runtime direction, but must be frictionless and first-class | Strong shadow git checkpoint system | **Gap** | Automatic checkpoints + restore + diff + undo UX | P0 | L |
| **Undo / rollback ergonomics** | Partial / explicit | Strong transparent model | **Gap** | One-step rollback for agent changes | P0 | M |
| **MCP support** | Needs to be first-class | Present and mature | **Gap** | Native MCP with discovery, policy, and good UX | P0 | L |
| **Reliability / fallback** | Some per-provider and shade model routing foundations | Mature operational behavior | **Gap** | Provider fallback, retries, degradation, health awareness | P0 | M |
| **Approval / safety controls** | Some scope enforcement, especially for shades | Stronger safety / approval maturity | **Gap** | Risk-aware approvals, policies, safer persistent agents | P0 | L |
| **Persistent named agents** | Core differentiator | Strong agent runtime, but not Charon-style persistent population as core identity | **Lead** | Deepen lifecycle, identity, specialization, continuity | P1 | M |
| **Direct inter-agent coordination** | Core differentiator, but needs more explicit operational protocols | Delegation exists; broader coordination is less central | **Lead** | Task board, leases, negotiations, dependency tracking | P1 | L |
| **Shades / bounded delegation** | Strong concept; phase-driven bounded workers | Hermes subagent delegation is real and mature | **Competitive** | Make shades clearly more reliable and inspectable than subagents | P1 | L |
| **Project knowledge as execution support** | Strong concept | Less central as explicit shared project layer | **Lead** | Operational project memory that changes behavior before actions | P1 | M |
| **TUI / session grid / operations console** | Strong differentiator; live session multiplexer | Hermes has a strong terminal UX, but not this operations layer | **Lead** | Make Charon the best agent operations console available | P1 | L |
| **Compaction / long-run coherence** | Foundations exist; needs more file/tool/goal-aware quality | Hermes strong enough to be competitive here | **Competitive** | Better continuity than Hermes on long-running work | P1 | L |
| **Procedures / skills** | Partial pieces, not yet dominant | Hermes skills system appears mature | **Gap** | Reusable procedures with learning, ranking, safety, versioning | P2 | XL |
| **Automation / scheduling** | Charon has scheduling and automation primitives | Hermes also strong here | **Competitive** | Goal-aware automations and better operational observability | P2 | L |
| **Research system (Libris)** | Major differentiator | Hermes strong in general research workflows, but not obviously equivalent to Libris architecture | **Lead** | Keep lead; tighten UX and integration with agent ops | P1 | M |
| **Judge loops** | Strong differentiator | Hermes may support optimization patterns, but judge loop is more explicit in Charon | **Lead** | Make judge loops dependable, visible, and composable | P1 | M |
| **External agent bridging** | Major differentiator via Charon’s Boat | Hermes not centered on hosting other agent systems | **Lead** | Deep structured bridges, not just pane embedding | P3 | XL |
| **Mixed-agent search / memory** | Partial foundations via bridge/import direction | Not a primary Hermes concern | **Lead** | Charon becomes the memory/control plane for heterogeneous agents | P3 | XL |
| **Cross-platform chat/messaging footprint** | More selective today | Hermes is very broad here | **Gap** | Only close where strategically useful; do not chase breadth blindly | P2 | L |
| **Developer workflow integrations** | Growing but not yet dominant | Hermes strong on generic surfaces; repo/dev loop differentiation still open | **Unknown** | Strong GitHub/PR/review/inbox/reporting flows | P2 | L |
| **Agent operating system framing** | Core Charon thesis | Hermes is a strong agent system, but not the same operating-layer bet | **Lead** | Make this visible in product quality, not just architecture docs | P1 | XL |

---

## Priority summary

## P0 — Must close early

These are the areas where Hermes appears stronger and where Charon will lose obvious comparisons if we do not improve quickly:

1. session recall quality
2. memory trust / provenance UX
3. browser / web operations
4. session search UX
5. transparent checkpoints
6. undo / rollback ergonomics
7. MCP support
8. runtime reliability / fallback
9. approval / safety controls

---

## P1 — Double down on Charon-native strengths

These are the areas where Charon can and should become clearly better than Hermes:

1. persistent named agents
2. inter-agent coordination
3. shades / bounded delegation
4. project knowledge as execution support
5. TUI / session grid / operations console
6. compaction / long-run coherence
7. Libris
8. judge loops
9. agent operating system productization

---

## P2 — Workflow superiority

These deepen day-to-day leverage once the foundations are strong:

1. procedures / skills
2. automation / scheduling
3. developer workflow integrations
4. selective external communication surfaces

---

## P3 — Strategic moat

These make Charon difficult to substitute:

1. deep external-agent bridges
2. mixed-agent search / memory
3. Charon as operating layer for heterogeneous agent fleets

---

## Where Charon should not overreact

Areas where we should be careful not to optimize for superficial comparison:

- broad messaging-platform parity without a clear product reason
- copying feature surfaces that do not strengthen the operating-system model
- trading reliability and observability for raw autonomy marketing

---

## Suggested ownership model

This matrix should eventually gain columns for:
- owner
- milestone
- benchmark task
- blocking dependency
- acceptance test

That version can become the execution dashboard for this plan.
