# Dashboard 3x3 Model

## Structure

The dashboard is a 3-row, 3-column layout.

Each row follows the same interaction grammar:
- left: selectable object list
- center: selected object details/stats
- right: selected object outputs/history

## Rows

### Row 1: Agents
- left: agent list
- center: selected agent details
- right: recent outcomes / ledger / actions

### Row 2: Projects
- left: project list
- center: selected project stats
- right: hierarchical goal tree

### Row 3: Automations
- left: automation list
- center: selected automation status/details
- right: recent runs / artifacts / logs

## Keyboard model
- `F2`: open dashboard
- `Tab` / `Shift-Tab`: cycle rows
- `Left` / `Right`: cycle columns in current row
- in left-column list panes, `Up` / `Down` move selected item
- in center/right panes, `Up` / `Down` move between rows

## Backend payload targets

Refresh payload should expose enough detail for each row:
- `agents`
- `projects` with usage, activity points, goal tree
- `automations` with runs tail and schedule metadata

Optional dashboard-specific wrapper:
- `dashboard.agents_row.items`
- `dashboard.projects_row.items`
- `dashboard.automations_row.items`

## Current implementation status
- Rust dashboard now follows the 3x3 structure.
- Project payload includes usage summary, activity points, and goal tree.
- Automation payload includes full state and recent runs.
- Command suggestions include automation commands.
