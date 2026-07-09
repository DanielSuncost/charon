# Charon Full Rust TUI Replacement Plan

**Status:** Proposed  
**Date:** 2026-03-25  
**Goal:** Build a full Rust replacement for the existing Charon TUI, preserving current workflows while upgrading the sessions view into a true live terminal multiplexer.

---

## Executive Summary

Build a **new standalone Rust frontend** for Charon that eventually replaces the current Bun/OpenTUI app.

The target is **feature parity plus one major improvement**:

- **Chat view:** same workflow, same backend, same streaming, same slash-command behavior
- **Dashboard view:** same information architecture, cleaner/faster native rendering
- **Sessions view:** upgraded from snapshot/polling UI to a **true live multi-terminal grid** using VTE-backed terminal panes

The key rule for this migration:

> **Do not modify or depend on the current Bun/OpenTUI frontend for runtime integration.**
> Build the Rust TUI as a separate app until it is good enough to replace the old one outright.

This avoids terminal-state conflicts, alt-screen ownership bugs, and mixed-renderer complexity.

---

## Product Definition

### What "done" means

A user can run:

```bash
charon
```

…and get the Rust TUI by default, with:

- **F1** Chat
- **F2** Dashboard
- **F3** Sessions

…and all three views feel like native parts of one app.

### What "parity" means

Not pixel-perfect reproduction of the old TUI.

Parity means:

- Same core workflows
- Same backend behavior
- Same setup/resume/provider flows
- Same important hotkeys and commands
- Same data visibility
- Same ability to steer, inspect, and manage agents

### What improves over the old TUI

The Sessions view becomes a **real terminal multiplexer**:

- multiple live sessions visible simultaneously
- full color/cursor/alt-screen support
- direct input to active pane
- explicit mode switch between grid navigation and terminal interaction
- remote sessions later supported through the same pane abstraction

---

## Non-Goals

These are **not required** for initial replacement readiness:

- Perfect visual replication of mascot/effects
- Every decorative animation from the current TUI
- Immediate remote session support on day one
- Full replacement of Python backend logic
- Rewriting agent runtime in Rust

The Rust TUI should remain a **frontend replacement**, not a backend rewrite.

---

## Architectural Principles

### 1. One terminal owner

The Rust TUI is the only process responsible for:

- alt screen
- raw mode
- cursor state
- render loop
- focus/input routing

No embedding Bun/OpenTUI inside it. No subprocess frontend mixing.

### 2. Preserve the Python backend

At first, the Rust TUI should talk to the existing backend protocol:

- spawn `apps/tui/opentui/chat_backend.py`
- read JSON lines from stdout
- send JSON lines to stdin

This minimizes product risk and keeps frontend migration independent.

### 3. Sessions are first-class terminals

Each session pane is a VTE terminal emulator, not a text snapshot.

### 4. Mode clarity beats cleverness

The sessions grid must clearly distinguish:

- **grid navigation mode**
- **terminal interaction mode**

This prevents key ambiguity.

### 5. Replace only when ready

The old TUI remains untouched as the production default until the Rust app reaches replacement criteria.

---

## User Experience Spec

## View Model

### F1 — Chat

Purpose: primary conversation interface with Charon.

Must support:

- streaming assistant responses
- user input box
- slash commands
- resume flow
- provider/model switching
- tool output display
- status/footer information
- scrolling transcript
- readable markdown rendering

### F2 — Dashboard

Purpose: system overview.

Must support:

- agent list
- selected agent details
- projects list
- recent activity / rear-view mirror
- token usage / provider/model context
- tasks / goals / assignments where applicable

### F3 — Sessions

Purpose: live multi-terminal supervision and interaction.

Must support:

- 1..N panes visible in grid layout
- focus movement between panes
- direct terminal interaction inside active pane
- live updates in inactive panes
- tmux/boat session discovery
- later: remote session streams

---

## Sessions View UX

### Two modes

#### A. Grid Navigation Mode

Keys:

- `Tab` / `Shift+Tab` → cycle panes
- arrows / `hjkl` → move focus by direction
- `Enter` → enter terminal mode for focused pane
- `z` → zoom/unzoom pane
- `n` → open/attach session picker
- `d` → detach/close pane (optional)
- `F1/F2` → switch views

Behavior:

- all panes continue updating
- selected pane has clear focus styling
- no keystrokes are sent to panes except explicit actions

#### B. Terminal Interaction Mode

Keys:

- all normal keys forwarded to active pane PTY/stream
- Enter, Tab, arrows, Ctrl+C, etc. behave like a real terminal
- exit terminal mode with a dedicated escape hatch

**Recommended escape hatch:** `Ctrl+]`

Why:

- safer than `Esc`
- less likely to conflict with terminal apps
- familiar as a terminal detach-style chord

Optional later:

- configurable escape chord
- double-Esc timeout

### Sessions success criteria

A user can:

- watch 4 agent sessions at once
- select a pane
- enter terminal mode
- type naturally as if in that terminal
- exit back to grid navigation without confusion
- switch away and back without pane state loss

---

## Technical Architecture

## Top-Level App

```rust
pub enum View {
    Chat,
    Dashboard,
    Sessions,
}

pub struct App {
    pub active_view: View,
    pub chat: ChatState,
    pub dashboard: DashboardState,
    pub sessions: SessionsState,
    pub global_status: GlobalStatus,
}
```

One render loop, one input loop, one app state.

---

## Chat Subsystem

### Backend bridge

Rust process spawns:

```text
apps/tui/opentui/chat_backend.py
```

Communicates via JSONL over stdio.

### Responsibilities

- send user input and slash commands
- receive streamed assistant tokens/events
- receive tool call events and status events
- render transcript incrementally
- manage resume/setup/provider state

### ChatState sketch

```rust
pub struct ChatState {
    pub transcript: Vec<ChatItem>,
    pub input: String,
    pub scroll: usize,
    pub mode: ChatMode,
    pub menu: Option<MenuState>,
    pub backend: BackendProcess,
    pub onboarding: OnboardingState,
    pub status: ChatStatus,
}
```

### Rendering requirements

- markdown-ish rendering
- code block rendering
- streaming cursor/partial message support
- tool/status blocks styled distinctly
- timestamps optional

### High Priority: Provider Switching, Context Transfer, and Session-Scoped Provider State

This is now a **must-preserve workflow**, not a polish item.

The Python backend already supports:

- provider switching via `/provider <name>` and `/setup provider <name>`
- automatic prompt to either continue with context transfer or start fresh
- session-scoped provider overrides
- rollback to the previous session provider when setup/auth/switch is interrupted
- transfer metadata/events/state exposed through backend refresh and status events

The Rust TUI must treat this as a **high-priority migration requirement**.

#### Backend rule

Do **not** reimplement transfer logic in Rust.

Reuse the existing backend behavior in:

- `apps/core-daemon/context_transfer.py`
- `apps/core-daemon/execution_memory.py`
- `apps/core-daemon/provider_bridge.py`
- `apps/tui/opentui/chat_backend.py`

Rust is responsible for **interaction, state presentation, and protocol handling**.

#### Required Rust TUI behaviors

- render provider-switch choice as a real modal/picker, not plain chat text
- support `↑/↓` selection
- support `Enter` to confirm
- support numeric quick-select (`1`, `2`) **without requiring Enter**
- never display `/1` and `/2` as the primary user-facing labels
- show clear progress while switching:
  - preparing transfer
  - switching provider
  - auth in progress when applicable
  - transfer applied
  - rollback/restored previous provider on failure
- display provider/model for the **active session**, not only from global onboarding state
- surface transfer metadata from refresh/session state where available

#### Session-state rule

The Rust TUI must not assume one global provider/model for the whole app.

At minimum, the active session view/header/status must reflect:

- session-specific provider
- session-specific model
- whether the current session was resumed via context transfer

#### Protocol requirements to preserve

Rust TUI must correctly support existing backend events used by this workflow:

- `status`
- `suggestions`
- `model_picker`
- `auth_url`
- `refresh` payload fields including:
  - `session_info.transfer`
  - `transfer_events`

#### Acceptance scenarios

The Rust TUI migration is **not complete** for chat/provider parity until these pass:

1. session A can remain on provider X while session B switches to provider Y
2. provider-switch chooser is keyboard-friendly and unambiguous
3. pressing `1` or `2` immediately selects the provider-switch option
4. interrupted provider switch restores the previous provider for that session
5. active session header/status always shows the correct provider/model after switching
6. resumed sessions visibly reflect transfer state somewhere in the UI

---

## Dashboard Subsystem

### DashboardState sketch

```rust
pub struct DashboardState {
    pub agents: Vec<AgentSummary>,
    pub projects: Vec<ProjectSummary>,
    pub selected_section: DashboardSection,
    pub selected_agent: usize,
    pub selected_project: usize,
    pub activity: Vec<ActivityEvent>,
}
```

### Rendering requirements

- fast native box layout
- keyboard-driven selection
- stable refresh from backend data

### Data source

Use the same backend refresh/event model the current TUI already uses.

---

## Sessions Subsystem

This is the system already started in Rust and should remain the foundation.

### SessionsState sketch

```rust
pub struct SessionsState {
    pub panes: Vec<SessionPane>,
    pub focused: usize,
    pub mode: SessionsMode, // GridNav | TerminalInput
    pub zoomed: Option<usize>,
    pub layout: GridLayout,
}
```

### Session pane

```rust
pub struct SessionPane {
    pub id: PaneId,
    pub title: String,
    pub terminal: TerminalState,
    pub parser: AnsiParser,
    pub backend: Box<dyn ByteStream>,
    pub connection_state: ConnectionState,
}
```

### Backends

Phase order:

1. `LocalPty`
2. `TmuxPane`
3. `RemoteStream` / SSH / TCP / WebSocket

---

## Backend Abstractions

```rust
pub trait ByteStream: Send {
    fn read_available(&mut self) -> io::Result<Vec<u8>>;
    fn write_bytes(&mut self, data: &[u8]) -> io::Result<usize>;
    fn resize(&mut self, width: u16, height: u16) -> io::Result<()>;
    fn is_eof(&self) -> bool;
}
```

### Implementations

- `PtyCapture`
- `TmuxPane`
- later `SshStream` / `BoatStream` / `TcpStream`

---

## Rendering Stack

### Current base

Already built:

- `terminal.rs`
- `parser.rs`
- `render.rs`
- `backend.rs`
- `session.rs`
- `grid.rs`

### Additional render work needed

- shared layout primitives
- transcript renderer
- dashboard widgets
- menu/picker widgets
- unified status/footer/header system

---

## Input Model

Global keys:

- `F1` → chat
- `F2` → dashboard
- `F3` → sessions
- `Ctrl+C` / `Ctrl+Q` → quit behavior as defined

View-local keys depend on active view.

Sessions mode-specific routing must be explicit.

---

## Migration Strategy

## Rule: Separate app until replacement is ready

Do **not** embed Rust inside Bun or Bun inside Rust.

Instead:

- keep current TUI untouched
- build new Rust app in parallel
- introduce a separate launcher command first:

```bash
charon-rust
```

Only when ready:

- switch `charon` to Rust by default
- optionally keep `charon-legacy`

---

## Milestones

## Milestone 0 — Freeze integration hacks

- no more Bun/Rust mixed runtime experiments
- keep current production TUI stable
- treat `crates/charon-tui` as the only frontend migration surface

### Exit criteria

- clear agreement on architecture

---

## Milestone 1 — Rust App Shell

Build a real app shell around the existing sessions code.

Deliverables:

- `App` state
- view switching (F1/F2/F3)
- top-level event loop
- placeholder chat/dashboard views
- status/footer/header framework

### Exit criteria

- one Rust binary with 3 native views
- F3 shows existing live sessions grid
- F1/F2 switch cleanly without terminal glitches

---

## Milestone 2 — Chat MVP

Build a usable Rust chat interface backed by `chat_backend.py`.

Deliverables:

- backend subprocess bridge
- transcript state
- streaming assistant rendering
- user input line
- slash command passthrough
- basic menus/status

### Exit criteria

- can hold a normal Charon conversation in Rust
- backend protocol works reliably
- no need to use Bun for basic chat workflow

---

## Milestone 3 — Dashboard MVP

Build the dashboard in Rust.

Deliverables:

- agents list
- projects list
- recent activity
- selected details
- token/provider/status info where available

### Exit criteria

- dashboard covers practical daily-use needs

---

## Milestone 4 — Sessions UX Completion

Build the final interaction model for sessions.

Deliverables:

- grid navigation mode
- terminal input mode
- explicit enter/exit terminal mode
- zoom support
- attach picker / discovery list
- better pane metadata
- waiting/approval indicators

### Exit criteria

- sessions view is decisively better than legacy F3

---

## Milestone 5 — Replacement Readiness

Port key remaining UX pieces:

- onboarding/setup
- resume picker
- provider/model picker
- session-scoped provider switching with context-transfer modal/progress/rollback UX
- hotkeys/help
- info panes if essential
- polishing and bug fixes

### Exit criteria

A normal user can use Rust TUI for a full day without needing the legacy TUI.

---

## Milestone 6 — Flip Default Launcher

- add `charon-rust` before flipping
- once stable, make `charon` launch Rust by default
- legacy Bun TUI remains available temporarily as fallback

---

## Replacement Readiness Checklist

The Rust TUI is ready to replace the old one when all are true:

### Chat
- [ ] streaming works
- [ ] slash commands work
- [ ] setup/resume flows work
- [ ] provider-switch chooser supports arrows, Enter, and immediate `1`/`2` selection
- [ ] session-specific provider/model display is correct for the active session
- [ ] provider-switch transfer progress and rollback states are visible
- [ ] tool/status events render clearly
- [ ] no transcript corruption under long sessions

### Dashboard
- [ ] agent/project/activity overview is complete enough for daily use
- [ ] keyboard navigation is smooth

### Sessions
- [ ] 4+ panes update live
- [ ] interactive apps render correctly
- [ ] terminal mode is reliable
- [ ] returning to grid mode is reliable
- [ ] attach/discovery works for charon and wrapped agents
- [ ] native Charon typing inside F3 does not flicker during live input
- [ ] F3 supports copying text from session panes (at least via a focused-pane copy mode)
- [ ] session-grid cells can show a brief current-task subtitle where available
- [ ] wrapped Hermes/pi boat sessions gain derived task summaries via output monitoring + Charon-style task inference

### Integration
- [ ] no Bun dependency required for frontend
- [ ] launcher support exists
- [ ] startup is stable
- [ ] resize behavior is stable
- [ ] no alt-screen/raw-mode glitches
- [ ] switching provider in one session does not mutate another session's effective provider/model

---

## Risks and Mitigations

### Risk: Chat parity becomes a long tail
**Mitigation:** ship MVP first; port by workflow priority, not by code parity.

### Risk: Markdown rendering quality lags behind current TUI
**Mitigation:** start with a simple renderer, improve incrementally. Workflow matters more than exact styling.

### Risk: Sessions mode key routing feels confusing
**Mitigation:** keep a strict mode distinction and use a dedicated escape chord.

### Risk: Dashboard data contract unclear
**Mitigation:** reuse existing backend refresh/event paths before redesigning.

### Risk: Remote sessions add too much scope
**Mitigation:** defer until local+tmux sessions are excellent.

---

## Recommended Immediate Next Step

Start **Milestone 1 + Milestone 2** together:

### Build “Rust Charon MVP”

Deliver:

- top-level Rust app shell
- native F1/F2/F3 switching
- current Rust sessions grid as F3
- chat backend bridge to `chat_backend.py`
- basic usable chat transcript + input as F1
- placeholder dashboard as F2

This gives a real end-to-end Rust Charon quickly, while leaving the legacy frontend untouched.

---

## Summary

The right path is:

1. keep current TUI stable
2. build a completely separate Rust frontend
3. preserve backend compatibility
4. use the already-working VTE sessions system as the foundation
5. replace the old TUI only when the Rust app is clearly superior

This is the cleanest path to a **drop-in replacement** instead of another fragile integration layer.
