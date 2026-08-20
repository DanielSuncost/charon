# F53: Upskill — Automated Agent Ecosystem Scanner

## Summary

A daily background process that scans major open-source agent repos for
updates, new capabilities, and patterns that could be incorporated into
Charon. Presents findings to the user as an actionable list of potential
skills/abilities, and polls for whether to adopt them.

## Motivation

Agent harnesses are moving fast. Features ship in Claude Code, Cursor,
Aider, Open Interpreter, Codex, Devin, SWE-agent, etc. on a weekly basis.
Without active monitoring, Charon falls behind on capabilities that users
expect. Upskill turns Charon into a self-improving system that stays
current with the ecosystem.

## How It Works

### 1. Daily Scan (background, low-cost)

A scheduled shade (or cron-triggered agent) runs once per day:

- **Repos to monitor** (configurable):
  - `anthropics/claude-code` — CLI features, MCP patterns, hooks
  - `paul-gauthier/aider` — edit formats, repo maps, git integration
  - `OpenInterpreter/open-interpreter` — OS integration, tool patterns
  - `princeton-nlp/SWE-agent` — trajectory patterns, evaluation
  - `All-Hands-AI/OpenHands` — sandbox patterns, delegation
  - `codestoryai/sidecar` — IDE integration patterns
  - User-added repos via config

- **What it checks**:
  - Git log since last scan (new commits, PRs merged)
  - Changelog / release notes
  - New or modified tool definitions
  - New command/slash-command patterns
  - Prompting techniques in system prompts
  - Architecture changes (new modules, new abstractions)

- **How it checks** (token-efficient):
  - `git log --oneline --since=<last_scan>` via local clones or GitHub API
  - Only fetch full diffs for commits that look relevant (keyword filter)
  - LLM summarization of relevant changes into structured findings

### 2. Findings Digest

Each finding is a structured record:

```yaml
- id: upskill-2026-04-12-001
  source: anthropics/claude-code
  commit: abc1234
  category: tool        # tool | prompt | architecture | ux | integration
  title: "Parallel tool execution with dependency graph"
  summary: "Claude Code now runs independent tool calls in parallel..."
  relevance: high       # high | medium | low
  effort: medium        # trivial | small | medium | large
  charon_gap: true      # does charon lack this?
  suggested_approach: "Add parallel execution to execute_tool loop..."
```

### 3. User Presentation

On startup (or via `/upskill`), show a digest:

```
 New Capabilities Detected (3 high, 5 medium)

  [adopt]  Parallel tool execution (claude-code, 2d ago)
  [adopt]  Repo-map context injection (aider, 1d ago)
  [skip]   Browser sandbox isolation (openhands, 3d ago)
  [defer]  Voice input pipeline (open-interpreter, 1d ago)

  adopt = create implementation task
  skip  = dismiss permanently
  defer = remind later
```

### 4. Adoption Flow

When user selects "adopt":
1. Create a detailed implementation brief (like the W1-W6 worker briefs)
2. Optionally auto-assign to a shade/worker for implementation
3. Track in the outcome ledger like any other task
4. On completion, the skill becomes part of Charon

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  Cron/Daily  │────▶│ Upskill Shade│────▶│ Findings DB  │
│  Trigger     │     │ (scan repos) │     │ (SQLite)     │
└─────────────┘     └──────────────┘     └──────┬───────┘
                                                │
                                    ┌───────────▼───────────┐
                                    │  /upskill command or   │
                                    │  startup digest in TUI │
                                    └───────────┬───────────┘
                                                │
                                    ┌───────────▼───────────┐
                                    │  User: adopt/skip/defer│
                                    └───────────┬───────────┘
                                                │ adopt
                                    ┌───────────▼───────────┐
                                    │  Implementation brief  │
                                    │  → shade/worker task   │
                                    └───────────────────────┘
```

## Config

```json
{
  "upskill": {
    "enabled": true,
    "scan_interval": "daily",
    "repos": [
      "anthropics/claude-code",
      "paul-gauthier/aider",
      "OpenInterpreter/open-interpreter"
    ],
    "auto_dismiss_low_relevance": true,
    "notify_on_high_relevance": true
  }
}
```

## Implementation Phases

### Phase 1: Manual scan
- `/upskill scan` command triggers a one-shot scan
- Scans configured repos via GitHub API
- Outputs findings as markdown in chat

### Phase 2: Scheduled + persistent
- Daily cron via shade scheduler
- Findings stored in SQLite
- `/upskill` shows unread findings
- adopt/skip/defer tracking

### Phase 3: Auto-implementation
- "adopt" creates a worker brief automatically
- Shade implements and opens PR for review
- User approves or rejects the implementation

## Open Questions

- Rate limiting: how many repos before GitHub API limits hit?
- Local clones vs API: clones are richer but use disk space
- Should low-relevance findings be auto-dismissed or shown?
- Integration with Libris: should upskill findings feed into research projects?
