# Dashboard Specification

> Reference: conversation with user on 2026-03-18

## Layout

Two rows, each with three columns, filling the full terminal height.

```
┌─────────────────┬──────────────────────┬─────────────────────┐
│  AGENTS LIST    │  AGENT INFO          │  AGENT REARVIEW     │
│                 │  ─────────────────── │                     │
│  [filter bar]   │  AGENT GOAL          │  Recent actions:    │
│  ▸ ● scout      │  ┌──────────────┐   │  • read main.py     │
│    ○ archivist  │  │ goal text    │   │  • edited config    │
│    ● test       │  │ [progress]   │   │  • ran tests        │
│                 │  └──────────────┘   │  • deployed v2      │
│                 │  [token meter]       │                     │
├─────────────────┼──────────────────────┼─────────────────────┤
│  PROJECTS LIST  │  PROJECT INFO        │  PROJECT AGENTS     │
│                 │                      │                     │
│  ▸ ◉ charon    │  Started: 2026-01    │  • scout (running)  │
│    ◎ demo      │  Path: /home/...     │  • test (idle)      │
│    ◉ webapp    │  Tokens: ████░░ 42k  │                     │
│                 │  Deploy: staging     │                     │
│                 │  Time: 12h active    │                     │
└─────────────────┴──────────────────────┴─────────────────────┘
```

## Agents Row

### Left Column: Agent List
- Vertical navigable list with arrow keys
- Filter bar at top: checkboxes/toggles for agent types
  - [x] Charon agents  [ ] Shades  [ ] External (boat)  [ ] Hidden
- Each entry shows: status icon (●○✖) + name + (role)
- Token usage meter: colored bar showing recent token consumption
- Enter on Charon agent or boat-connected agent → join session
- Hotkey (e.g., 'a') on highlighted agent → assign to project
- Tab switches focus to Projects row

### Middle Column (split vertically):
- **Top half: Agent Info**
  - ID, role, mode, status, project affiliation
  - Provider/model being used
- **Bottom half: Agent Goal**
  - Current goal text
  - Progress indicator if available
  - Token-over-time meter (colored sparkline or bar)

### Right Column: Agent Rearview
- Short text descriptions of highlighted agent's recent actions
- From agent's inbox/attempts log
- Auto-updates when selection changes

## Projects Row

### Left Column: Project List
- Vertical navigable list
- Activity icon: ◉ (active/being worked on) ◎ (idle)
- Metric badge: token count or time spent
- Enter → open session grid filtered to this project

### Middle Column: Project Info
- Project name, path, date started
- Deployment state (if tracked)
- Token usage across time (colored meter/bar)
- Total time spent by agents

### Right Column: Project Agents
- List of agents associated with this project
- Status indicator for each
- Shows which are currently active on the project

## Commands

- `/project new "name"` — create a new project (no provider needed)
- `/project add-agent <agent-id>` — assign agent to project
- `/project remove-agent <agent-id>` — unassign agent
- `/project list` — list all projects
- `/agent hide <agent-id>` — hide from dashboard
- `/agent show <agent-id>` — unhide

## Hotkeys (in dashboard)

- ↑↓ — navigate current list
- Tab — switch between agents row and projects row
- Enter — join session (agent) or open session grid (project)
- 'a' — assign highlighted agent to a project (opens project picker)
- 'f' — toggle filter options
- F1 — back to chat
- F3 — sessions view

## Implementation Phases

### Phase 1: Data model (backend)
- [ ] Add project registry to chat_backend.py (create/list/add-agent/remove-agent)
- [ ] Add agent activity log reader (recent actions from inbox/attempts)
- [ ] Add token tracking stubs (placeholder metrics for now)
- [ ] Wire refresh payload to include all needed data

### Phase 2: Two-row layout with real columns
- [ ] Since OpenTUI can't do true side-by-side text in a single Text renderable,
      and multi-Box instantiate layout works (proven in column test), build the
      dashboard as a SEPARATE view with its own instantiated Box/Text tree
- [ ] Instead of swapping mainText.content, swap which tree is attached to root
- [ ] Agent list column with selection cursor
- [ ] Agent info + goal column
- [ ] Agent rearview column

### Phase 3: Projects row
- [ ] Project list column
- [ ] Project info column with metrics
- [ ] Project agents column

### Phase 4: Interactions
- [ ] Enter to join session
- [ ] 'a' to assign agent to project
- [ ] Filter toggles for agent types

### Phase 5: Metrics & polish
- [ ] Token usage meters (colored bars)
- [ ] Activity sparklines
- [ ] Deployment state tracking
- [ ] Time tracking
