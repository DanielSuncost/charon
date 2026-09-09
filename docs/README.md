# docs

Documentation index for Charon.

## Top-level docs

| Document | Description |
|---|---|
| [install.md](install.md) | Installing Charon on macOS and Ubuntu |
| [sessions-and-daemon.md](sessions-and-daemon.md) | How sessions persist in the local daemon (`charond`) |
| [three-tier-memory.md](three-tier-memory.md) | The user / project / agent memory hierarchy |
| [cross-agent-threads.md](cross-agent-threads.md) | Cross-agent decision and discussion threads (who/when/why) |
| [remote-agent-teams.md](remote-agent-teams.md) | Persistent agent teams on remote machines (Harbor) |
| [agent-abilities-registry.md](agent-abilities-registry.md) | Auto-generated registry of agent abilities |
| [review-artifact.md](review-artifact.md) | The standard review artifact: findings, severity, evidence, test results, unresolved risks |
| [design-library-spec.md](design-library-spec.md) | Design library spec |
| [onboarding-summary.md](onboarding-summary.md) | Onboarding design and status summary |
| [manual-provider-switch-transfer-test-checklist.adoc](manual-provider-switch-transfer-test-checklist.adoc) | Manual test checklist for provider-switch context transfer |
| [proposals/charon-workspace.md](proposals/charon-workspace.md) | Proposed institutional context and coordinated-delivery control plane |

## Subdirectories

| Directory | Contents |
|---|---|
| [architecture/](architecture/) | System overviews (Charon, Charon's Boat, session connectivity) |
| [adr/](adr/) | Architecture decision records |
| [contracts/](contracts/) | JSON schemas and command/event contracts |
| [features/](features/) | Feature specs (see [features/INDEX.md](features/INDEX.md)) |
| [proposals/](proposals/) | Product and architecture proposals under review |

## Skills

[`skills/`](../skills/) (repo root, not under `docs/`) holds the engineering practice an
agent working in this repo is expected to follow — debugging, TDD, spikes, code review,
exploratory QA, and the system map. See [skills/README.md](../skills/README.md).

## Plans

[plans/](plans/) holds live planning documents — notably
[capability-roadmap.md](plans/capability-roadmap.md) and
[MASTER_PLAN.md](plans/MASTER_PLAN.md).

[plans/archive/](plans/archive/) holds historical and superseded plans,
kept for reference: completed worker briefs, proposals that shipped in a
different form, and designs replaced by what was actually built.
