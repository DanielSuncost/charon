# Rust TUI Migration Plan for Charon

**Status:** Proposed | **Date:** 2026-03-24 | **Trigger:** Session-grid pain point + VTE terminal emulator discovery

---

## Executive Summary

Adopt a **VTE-based terminal emulator architecture inspired by FrankenTUI** to solve the session-grid UX problem. Each grid cell becomes a live, interactive terminal with full ANSI support (colors, cursor, scrollback), fed by continuous PTY byte streams rather than polled snapshots. This cleanroom approach validates the terminal emulator primitive first, then scales out with multi-pane layouts and advanced visual effects.

---

## Critical Discovery: FrankenTUI Has Real VTE Terminals ✅

### The Problem With Current Architecture

Your current session-grid uses a **poll-and-scrape** model:
1. `setInterval` polls every 500ms–3s
2. Python backend calls `tmux capture-pane` (static text snapshot)
3. Captured text gets ANSI-stripped, filtered, word-wrapped
4. Rendered as `dim(content)` inside hand-drawn Unicode box borders

**Result:** This is fundamentally a **screenshot viewer**, not a terminal. You lose:
- Colors (ANSI stripped)
- Cursor position and blink
- Interactive TUI apps (vim, htop render as garbage)
- Real-time updates (500ms–3s latency on every visual change)

### The FrankenTUI Solution

FrankenTUI uses the **`vte` crate** for ANSI parsing with a full terminal state machine that maintains:
- **Character grid** with per-cell colors and attributes
- **Cursor position** with shape tracking (block/bar/underline)
- **Scrollback buffer** with line flags (soft-wrap vs hard-newline)
- **Continuous byte stream processing** via `PtyCapture`

This means each session can be a **real terminal emulator**, not just a text buffer viewer.

---

## Architecture Comparison

| Aspect | Current (Poll-and-Scrape) | Revised Plan (VTE Terminal) |
|--------|--------------------------|----------------------------|
| **Data Source** | `tmux capture-pane` snapshots | Continuous PTY byte stream |
| **Parsing** | ANSI strip + word wrap | Full VTE state machine |
| **Colors** | Lost on strip | Full 256/truecolor preserved |
| **Cursor** | Not tracked | Live position + blink phase |
| **Interactive Apps** | Render as garbage | vim, htop work correctly |
| **Latency** | 500ms–3s poll interval | Continuous stream (real-time) |
| **Scrollback** | Text lines only | Full terminal history with attrs |

---

## Key FrankenTUI Components to Port

| Component | Source File | Lines | Purpose |
|-----------|-------------|-------|---------|
| `TerminalState` | `ftui-extras/src/terminal/state.rs` | 2.7K | Grid + cursor + scrollback state machine |
| `AnsiParser` | `ftui-extras/src/terminal/parser.rs` | 2.6K | VTE-based escape sequence parser |
| `TerminalEmulator` widget | `ftui-extras/src/terminal/widget.rs` | 1.1K | Renders terminal state to buffer |
| `PtyCapture` | `ftui-extras/src/pty_capture.rs` | 692 | Continuous PTY byte stream reader |

---

## Phase 1: Single Live Terminal Cell (Weeks 1-2)

### Goal
Get **one cell, one live terminal, full fidelity**. Not pane trees, not workspace snapshots, not tab switching. Just a single grid cell that behaves like a real terminal with colors, cursor, scrollback, and interactive apps working correctly.

### Components to Adapt

#### 1.1 `TerminalState` - The VTE Grid
**Source:** `frankentui/crates/ftui-extras/src/terminal/state.rs` (2.7K lines)

```rust
pub struct TerminalState {
    pub grid: Vec<Cell>,           // flat buffer: width × height, indexed as [y * width + x]
    pub cursor: Cursor,            // position + shape (block/bar/underline)
    pub scrollback: VecDeque<Line>,// historical lines with wrap flags
    pub dimensions: (u16, u16),    // current terminal size
    pub dirty: bool,               // true if grid changed since last render
}

pub struct Cell {
    pub ch: char,                  // character (or WIDE_CONTINUATION sentinel)
    pub fg: Option<Color>,         // foreground color
    pub bg: Option<Color>,         // background color  
    pub attrs: CellAttrs,          // bold, dim, italic, underline, etc.
}
```

**Why:** This is the heart of the terminal emulator. Every ANSI escape sequence updates this state machine. The grid holds the current viewport; scrollback holds history with line flags (soft-wrap vs hard-newline) for correct copy extraction later.

**Note on flat buffer:** Using a single `Vec<Cell>` instead of `Vec<Vec<Cell>>` gives better cache locality and fewer heap allocations - critical when blitting multiple cells at 60fps.

#### 1.2 `AnsiParser` - VTE-Based Escape Sequence Parser
**Source:** `frankentui/crates/ftui-extras/src/terminal/parser.rs` (2.6K lines)

```rust
use vte::{Parser, Perform};

pub struct AnsiParser {
    parser: vte::Parser,
    handler: Box<dyn AnsiHandler>,  // dispatches to TerminalState
}

impl AnsiParser {
    pub fn print(&mut self, bytes: &[u8]) {
        for byte in bytes {
            self.parser.advance(self, *byte);
        }
    }
}
```

**Why:** The `vte` crate handles all the complexity of ANSI parsing (CSI sequences, OSC strings, DCS hooks, UTF-8 multi-byte handling). You feed it raw PTY bytes; it dispatches events to your handler which updates `TerminalState`. No manual escape sequence parsing needed.

#### 1.3 Backend Abstraction - Continuous Byte Stream Readers
**Source:** `frankentui/crates/ftui-extras/src/pty_capture.rs` (692 lines) + tmux integration

```rust
pub trait ByteStream: Send {
    fn read_available(&mut self) -> io::Result<Vec<u8>>;
    fn write(&mut self, data: &[u8]) -> io::Result<usize>;
    fn is_eof(&self) -> bool;
    fn resize(&mut self, width: u16, height: u16) -> io::Result<()>;  // For SIGWINCH/resize-pane
}

// Option A: Spawn new PTY subprocess (portable-pty)
pub struct PtyCapture {
    child: Box<dyn portable_pty::Child + Send + Sync>,
    writer: Box<dyn Write + Send>,
    rx: mpsc::Receiver<ReaderMsg>,  // Data, Eof, Err messages
}

// Option B: Attach to existing tmux pane (pipe-pane) - RECOMMENDED FOR PHASE 1
pub struct TmuxPipe {
    pane_id: String,
    fifo_path: PathBuf,       // /tmp/pane-N.fifo
    reader: File,             // Read end of pipe
    writer: OsPipeWrapper,    // Write end (for keystrokes)
}

impl ByteStream for PtyCapture { /* ... */ }
impl ByteStream for TmuxPipe { /* ... */ }
```

**Why:** Replaces the poll-and-scrape model. Instead of calling `capture-pane` every 500ms, you read from a channel that continuously receives PTY output as it arrives. Zero polling latency.

**Phase 1 recommendation:** Start with `TmuxPipe` to attach to existing agent sessions in tmux. This lets you test against real agents immediately without spawning new processes.

#### 1.4 `TerminalEmulator` Widget - Render to Parent Terminal
**Source:** `frankentui/crates/ftui-extras/src/terminal/widget.rs` (1.1K lines)

```rust
pub struct TerminalEmulator {
    show_cursor: bool,
    cursor_visible_phase: bool,  // for blink animation
}

impl Widget for TerminalEmulator {
    fn render(self, area: Rect, frame: &mut Frame, state: &TerminalEmulatorState) {
        // Blit terminal.grid into parent terminal at (x,y) offset
        // Apply cursor styling if visible and in bounds
    }
}
```

**Why:** This is the "blitting" step - copying the VTE grid into the right rectangle of the outer terminal. Each cell's `(char, fg, bg, attrs)` becomes a buffer cell at the appropriate offset using cursor positioning escape sequences.

### Main Loop Architecture

```rust
pub struct SessionCell {
    pub terminal: TerminalState,  // The VTE grid state
    pub backend: Box<dyn ByteStream>,  // PtyCapture or TmuxPipe
    pub parser: AnsiParser,       // Escape sequence parser
    pub area: Rect,               // Where to render in parent terminal
}

// Main loop feeds bytes continuously (no polling!)
loop {
    // 1. Read available PTY output (non-blocking)
    let bytes = cell.backend.read_available()?;
    
    // 2. Feed to VTE parser → updates TerminalState + sets dirty flag
    if !bytes.is_empty() {
        cell.parser.print(&bytes);
        cell.terminal.dirty = true;
    }
    
    // 3. Handle resize events (from parent terminal or layout change)
    if let Some(new_size) = poll_resize_event(cell.area) {
        cell.backend.resize(new_size.width, new_size.height)?;
        cell.terminal.resize(new_size.width, new_size.height);
        cell.terminal.dirty = true;  // Content may reflow
    }
    
    // 4. Render only if dirty (optimization vs blind 60fps)
    if cell.terminal.dirty {
        render_terminal(cell.terminal, cell.area);
        cell.terminal.dirty = false;
    }
    
    // 5. Handle user input (forward keystrokes to backend)
    if let Some(key) = poll_key_event() {
        cell.backend.write(&key.to_bytes())?;
        cell.terminal.dirty = true;  // Input may change display
    }
    
    // 6. Frame timing (~30-60fps, adaptive based on dirty state)
    sleep(Duration::from_millis(16));
}
```

**Resize propagation:** When the outer terminal resizes or layout changes:
1. Detect new `area` dimensions for each cell
2. Call `backend.resize()` to send SIGWINCH (PTY) or resize-pane (tmux)
3. Update `TerminalState.dimensions` and let VTE reflow content
4. Set dirty flag to trigger re-render

### Success Criteria
- [ ] Single grid cell renders live terminal output with full colors
- [ ] Cursor position and blink work correctly
- [ ] Interactive apps (vim, htop) render properly inside the cell
- [ ] Keystrokes forwarded to PTY respond in real-time (<50ms latency)
- [ ] Scrollback preserves history with correct line wrapping
- [ ] Resize events propagate to backend and reflow content correctly

---

## Python Backend Integration Options

Before building Phase 1, decide how the Rust TUI connects to your existing stack:

### Option A: Full Replacement (Rust handles everything)
- Rust binary spawns `chat_backend.py` as subprocess
- Communicates via JSON-over-stdio (same as current Bun process)
- **Pros:** Clean migration path, single binary deployment
- **Cons:** Need to re-implement chat logic in Rust eventually

### Option B: Sidecar Mode (Rust handles sessions only)
- Existing Python/Bun stack continues handling chat
- Rust TUI spawns alongside, connects to same agent sessions via tmux
- **Pros:** Minimal disruption, can validate VTE terminal primitive independently
- **Cons:** Two processes coordinating; need IPC for sync

### Option C: Wrapper Mode (Rust wraps Python)
- Rust TUI spawns and manages `chat_backend.py`
- Python continues handling all chat/session logic
- Rust only renders the session grid view
- **Pros:** Leverages existing Python backend fully
- **Cons:** Less control over terminal rendering layer

**Phase 1 Recommendation:** Start with **Option B (Sidecar)** - attach to existing tmux sessions via `TmuxPipe`. This validates the VTE primitive without disrupting your current chat flow. You can migrate to Option A later once Rust TUI is stable.

---

## Phase 2: Multi-Cell Grid Layout & Tab Switching (Weeks 3-4)

### Goal
Scale from one cell to a grid of live terminals with tab switching. Now that the terminal emulator primitive works, add the **PaneTree** model for layout management and focus tracking.

### Components to Adapt

#### 2.1 `PaneId` + `PaneTree` Data Structures
**Source:** `frankentui/crates/ftui-layout/src/pane.rs` (3.4K lines)

```rust
pub struct PaneId(u64);  // Stable identifier per session

pub enum PaneNodeKind {
    Leaf(PaneId),           // Single terminal cell
    Split {                 // Multi-pane layout (horizontal/vertical)
        axis: SplitAxis,
        children: Vec<PaneNode>,
        ratio: f32,         // Space allocation between panes
    },
}

pub struct PaneTree {
    root: PaneNode,
    panes: HashMap<PaneId, TerminalState>,  // Map IDs to terminal states
}
```

**Why:** Stable IDs prevent re-rendering entire UI on tab switch. Each session maintains its own `TerminalState` independently (grid, cursor, scrollback). The tree structure enables nested layouts (e.g., split-pane editors) later.

#### 2.2 `WorkspaceSnapshot` with Active Pane Tracking
**Source:** `frankentui/crates/ftui-layout/src/workspace.rs`

```rust
pub struct WorkspaceSnapshot {
    pub pane_tree: PaneTree,
    pub active_pane_id: Option<PaneId>,  // Currently focused session
    pub generation: u64,                  // For determinism/replay
}
```

**Why:** Tab switching becomes a single field change (`active_pane_id`) instead of full state reconstruction. The `generation` counter enables deterministic replay later if needed (e.g., for debugging or audit logs).

#### 2.3 Focus-Aware Render Loop
**Source:** `frankentui/crates/ftui-runtime/src/render_trace.rs`

```rust
pub fn render_frame(workspace: &WorkspaceSnapshot, area: Rect) -> Frame {
    let mut frame = Frame::new(area);
    
    for (pane_id, terminal_state) in workspace.pane_tree.all_panes() {
        if Some(pane_id) == workspace.active_pane_id {
            // Full fidelity render with cursor, colors, animations
            render_terminal_full(terminal_state, &mut frame);
        } else {
            // Minimal render or skip (cached state preserved)
            // Still update internal state, just don't fully blit
            update_terminal_state(terminal_state);
        }
    }
    
    frame
}
```

**Why:** Reduces CPU load when inactive sessions don't need full rendering. Critical for smooth tab switching with many sessions open. Inactive terminals continue receiving PTY bytes and updating their state machines - they just render at lower fidelity until focused again.

#### 2.4 Input Routing & Keystroke Forwarding
**Source:** `frankentui/crates/ftui-runtime/src/input.rs` (adapted)

```rust
pub fn handle_input(key: KeyEvent, workspace: &mut WorkspaceSnapshot) {
    match key.code {
        KeyCode::Tab => {
            // Cycle to next pane
            workspace.active_pane_id = next_pane(workspace);
        }
        _ if is_special_key(&key) => {
            // Handle global shortcuts (quit, resize, etc.)
            handle_global_shortcut(key, workspace);
        }
        _ => {
            // Forward to active pane's PTY
            if let Some(pane_id) = workspace.active_pane_id {
                let pty = get_pty_for_pane(pane_id);
                pty.write(&key.to_bytes())?;
            }
        }
    }
}
```

**Why:** Separates global UI shortcuts (tab switching, quit) from session-specific input (keystrokes forwarded to PTY). Enables keyboard-driven workflow without mouse.

### Success Criteria
- [ ] Can render 2×2 grid of live terminals simultaneously
- [ ] Tab key cycles focus between cells smoothly (<50ms switch latency)
- [ ] Each cell maintains independent scrollback and cursor state
- [ ] Inactive cells continue receiving PTY updates (state accumulates)
- [ ] Returning to inactive cell shows accumulated output correctly

---

## Phase 3: Remote Sessions & Network Backends (Month 2)

### Goal
Monitor and interact with sessions running on remote servers as if they were local. The key insight: **the VTE parser doesn't care where bytes come from** - PTY, TCP socket, WebSocket, or file. Same terminal emulator state machine works for all backends.

### Components to Adapt

#### 3.1 Unified Session Backend Abstraction
**Source:** `frankentui/crates/ftui-extras/src/pty_capture.rs` + network adapters

```rust
pub trait ByteStream: Send {
    fn read_available(&mut self) -> io::Result<Vec<u8>>;
    fn write(&mut self, data: &[u8]) -> io::Result<usize>;
    fn is_eof(&self) -> bool;
}

pub enum SessionBackend {
    LocalPty(PtyCapture),      // portable-pty backed subprocess
    TcpStream(TcpSession),     // Raw TCP connection to remote shell
    WebSocket(WsSession),      // WebSocket for web adapter compatibility
    TmuxPane(TmuxPipe),        // tmux pipe-pane for existing tmux workflows
}

impl ByteStream for SessionBackend { /* unified interface */ }
```

**Why:** Same abstraction works for local terminal processes and remote streams. The `TerminalState` + `AnsiParser` combination is backend-agnostic - it just consumes bytes. Enables seamless switching between local and remote sessions with identical UX.

#### 3.2 tmux pipe-pane Integration (Optional High-Impact)
**Source:** Custom adapter for existing tmux infrastructure

```rust
pub struct TmuxPipe {
    pane_id: String,
    fifo_path: PathBuf,       // /tmp/pane-N.fifo
    reader: File,             // Read end of pipe
    writer: OsPipeWrapper,    // Write end (for keystrokes)
}

impl TmuxPipe {
    pub fn attach(pane_id: &str) -> io::Result<Self> {
        // tmux pipe-pane -p <pane> -o "cat > /tmp/pane-N.fifo"
        // Then read/write the FIFO continuously
    }
}
```

**Why:** If you already have sessions in tmux, `pipe-pane` gives you the raw byte stream without spawning new processes. Lower overhead than full PTY spawn for existing workflows.

#### 3.3 Evidence Events for Deterministic Replay
**Source:** `frankentui/crates/ftui-runtime/src/render_trace.rs` + `crates/ftui-harness/src/determinism.rs`

```rust
pub enum EvidenceEvent {
    BytesReceived { 
        pane_id: PaneId, 
        timestamp: Instant,
        byte_count: usize,
    },
    UserInput { 
        pane_id: PaneId, 
        key: KeyCode,
        timestamp: Instant,
    },
    ScrollPositionChanged { 
        pane_id: PaneId, 
        offset: usize,
        timestamp: Instant,
    },
    FocusChanged { 
        from: Option<PaneId>, 
        to: Option<PaneId>,
        timestamp: Instant,
    },
}

// Events logged to JSONL for later replay/analysis/debugging
```

**Why:** Remote sessions can be audited and debugged. If a session misbehaves (e.g., cursor in wrong position after network blip), you can replay its event log to understand what happened. Critical for production debugging.

#### 3.4 Connection State & Reconnection Logic
Visual indicator showing remote session status with auto-reconnect behavior.

```rust
pub enum ConnectionState {
    Connecting,
    Connected,
    Disconnected { reason: String, timestamp: Instant },
    Reconnecting { attempt: u32, next_try_in: Duration },
}

// Visual indicator in pane header/border
// 🟢 Connected | 🟡 Reconnecting (3s) | 🔴 Disconnected
```

**Why:** Network sessions fail differently than local PTY. Auto-reconnect with visual feedback prevents user confusion when network blips occur.

### Success Criteria
- [ ] Can open session connected to remote server via TCP/WebSocket
- [ ] Session survives network blips with auto-reconnect (<5s recovery)
- [ ] Visual indicator shows connection state clearly (color-coded border/icon)
- [ ] Switching between local and remote sessions feels identical
- [ ] Evidence events logged to JSONL for post-mortem analysis

---

## Phase 4: Visual Polish & Advanced Effects (Month 2+)

### Goal
Add "delight" features that make the TUI feel premium. These are **optional enhancements** that build on the solid VTE foundation - don't add them until Phases 1-3 are stable.

### Components to Adapt

#### 4.1 Canvas with Braille Dithering (High-Impact)
**Source:** `frankentui/crates/ftui-extras/src/canvas.rs` (57K lines)

```rust
pub enum Mode {
    Block,      // Standard terminal cells (1:1 mapping)
    Braille,    // High-res dithering using Braille dots (2×4 grid per cell = 8x resolution)
}

// For the mascot's lantern or smooth gradients:
let mut painter = Painter::new(10, 10, Mode::Braille);
painter.fill_circle(cx, cy, radius, color);
```

**Why:** Braille mode gives **8× resolution within a single terminal cell**. Perfect for:
- Smooth circular shapes (mascot's lantern glow)
- Gradient transitions without banding
- Small icons that need anti-aliasing

**Trade-off:** Requires terminals with good Braille rendering. Not all fonts render Braille dots cleanly - test on your target terminal first.

#### 4.2 Text Effects Library
**Source:** `frankentui/crates/ftui-extras/src/text_effects.rs` (383K lines)

Key effects to adapt:

| Effect | Use Case in Charon | Complexity |
|--------|-------------------|------------|
| `Pulse { speed, min_alpha }` | Mascot's lantern shimmer, notification badges | Low |
| `FadeIn { progress }` | New message appearance, smooth transitions | Low |
| `RainbowGradient { speed }` | Fun mode for agent names or headers | Medium |
| `Glitch { intensity }` | Error states, disconnection warnings | Medium |

```rust
// Lantern shimmer example:
let lantern = StyledText::new("🏮")
    .effect(TextEffect::Pulse { 
        speed: 2.0,      // cycles per second
        min_alpha: 0.3   // minimum brightness (0.0-1.0)
    })
    .base_color(PackedRgba::rgb(255, 180, 50))  // Orange-gold
    .time(current_time);
```

**Why:** Adds personality and visual feedback without heavy computation. The `Pulse` effect is perfect for the lantern shimmer - it uses an asymmetric breathing curve (quick inhale, slow exhale) that feels organic rather than mechanical.

#### 4.3 Inline vs Alt-Screen Mode Toggle
**Source:** `frankentui/crates/ftui-demo-showcase/src/screens/inline_mode.rs`

```rust
pub enum DisplayMode {
    Inline,     // UI at bottom of terminal, shell history scrolls above
    AltScreen,  // Full-screen dedicated mode (clears shell history)
}

// Toggle with F11 or Ctrl+L
```

**Why:** Lets users choose their workflow:
- **Inline mode:** Preserve command history while keeping chat interface pinned at bottom. Great for power users who want to see tool output alongside agent responses.
- **Alt-screen mode:** Full immersion when you want Charon to take over the entire terminal.

#### 4.4 Mermaid Diagram Rendering (Optional)
**Source:** `frankentui/crates/ftui-extras/src/diagram.rs`

Render ASCII/Unicode diagrams from Mermaid syntax for visualizing agent workflows or data structures.

### Success Criteria
- [ ] Mascot lantern has smooth shimmer effect (no stutter, 60fps)
- [ ] New messages fade in smoothly (<200ms transition)
- [ ] Connection state changes have visual feedback (color-coded indicators)
- [ ] Braille dithering renders cleanly on supported terminals (kitty, alacritty, wezterm)
- [ ] Inline/alt-screen mode toggle works without data loss

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                      Charon TUI                             │
├─────────────────────────────────────────────────────────────┤
│  WorkspaceSnapshot                                          │
│  ├─ PaneTree (session layout: Leaf | Split)                │
│  ├─ active_pane_id (focused session)                       │
│  └─ generation counter (for deterministic replay)          │
├─────────────────────────────────────────────────────────────┤
│  Session Manager [per PaneId]                               │
│  ├─ TerminalState                                          │
│  │   ├─ grid: Vec<Cell> (flat buffer, indexed [y*w+x])    │
│  │   ├─ cursor: {x, y, shape, visible}                    │
│  │   └─ scrollback: VecDeque<Line> with wrap flags         │
│  ├─ AnsiParser (vte crate-based)                           │
│  │   └─ dispatches escape sequences → TerminalState        │
│  ├─ SessionBackend (ByteStream trait)                      │
│  │   ├─ LocalPty (portable-pty subprocess)                │
│  │   ├─ TcpStream (remote shell)                          │
│  │   ├─ WebSocket (web adapter)                           │
│  │   └─ TmuxPipe (tmux pipe-pane integration)             │
│  └─ Input routing (keystrokes → PTY write)                 │
├─────────────────────────────────────────────────────────────┤
│  Render Loop (60fps target)                                 │
│  ├─ For each pane in PaneTree:                             │
│  │   ├─ Read available bytes from Backend                  │
│  │   ├─ Feed to AnsiParser → update TerminalState          │
│  │   └─ Blit grid to parent terminal at pane.area         │
│  ├─ Focus-aware optimization (full render active only)     │
│  ├─ Canvas painter (Braille mode for high-res effects)     │
│  └─ Text effects engine (pulse, fade-in, glitch, etc.)     │
├─────────────────────────────────────────────────────────────┤
│  Event Handler                                              │
│  ├─ Global shortcuts (Tab → cycle focus, F11 → toggle mode)│
│  ├─ Keystroke forwarding to active pane's Backend          │
│  ├─ Resize propagation (update all TerminalState dims)     │
│  └─ Evidence event logging (JSONL for replay/debug)        │
└─────────────────────────────────────────────────────────────┘
```

### Data Flow: Single Frame Cycle

```
1. Backend.read_available() → Vec<u8> (PTY/TCP/WebSocket bytes)
2. AnsiParser.print(bytes) → dispatches to TerminalState
3. TerminalState updates grid, cursor, scrollback
4. TerminalEmulator.render(area, state) → blits to parent terminal
5. Input.poll() → keystrokes written back to Backend.write()
6. Loop at 60fps (16ms frame time)
```

### Key Invariants

1. **Cell mapping:** Terminal cells map 1:1 to buffer cells within the area
2. **Cursor visibility:** Cursor renders only when visible and within bounds
3. **Resize propagation:** Resize events update both `TerminalState` dimensions and `Backend` PTY size
4. **Scrollback limit:** Never exceeds configured max (oldest lines dropped first)
5. **Focus isolation:** Only active pane receives keystrokes; all panes receive backend bytes

---

## Risk Assessment

### Technical Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| VTE parser overhead on high-throughput sessions | Medium | Profile Phase 1; `vte` crate is battle-tested and fast |
| Braille dithering not supported on all terminals | Low | Fallback to block mode; document terminal requirements (kitty, alacritty, wezterm preferred) |
| Remote session latency feels different from local PTY | Medium | Add connection indicator; tune reconnection logic with exponential backoff |
| UTF-8 multi-byte character handling edge cases | Low | `vte` crate handles this correctly; test with CJK characters early |

### Scope Creep Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Building pane layout before validating VTE terminal primitive | High | **Stick to Phase 1 first** - get one cell working perfectly before adding grid |
| Over-engineering remote session support early | Medium | Defer to Phase 3; focus on local PTY UX first |
| Adding visual effects before core stability | Medium | Gate Phase 4 behind "all Phases 1-3 success criteria met" checkpoint |

---

## Decision Log

### Why VTE Terminal Emulator vs. Text Buffer Viewer?

**Decision:** Use full VTE state machine per cell, not just text lines.

**Rationale:**
1. **Colors preserved** - Current architecture strips ANSI; VTE maintains per-cell fg/bg
2. **Cursor tracked** - Essential for interactive apps (vim, htop) to render correctly
3. **Real-time updates** - Continuous byte stream vs. 500ms-3s poll interval
4. **Proven in FrankenTUI** - Same `vte` crate + `TerminalState` pattern already working

### Why FrankenTUI as Reference Architecture?

**Decision:** Adapt FrankenTUI's terminal emulator stack, not just pane model.

**Rationale:**
1. **VTE-based parsing** - Uses industry-standard `vte` crate for ANSI escape sequences
2. **Continuous PTY streams** - `PtyCapture` provides non-blocking byte reading (no polling)
3. **TerminalState state machine** - Maintains grid, cursor, scrollback with proper invariants
4. **Widget-based rendering** - `TerminalEmulator` widget handles blitting to parent terminal cleanly

### Why Not Just Use Alacritty or Kitty Directly?

**Decision:** Build on FrankenTUI's abstraction layer first.

**Rationale:**
1. **Cleaner integration** - FrankenTUI already wraps `vte` + PTY handling; less boilerplate
2. **Proven patterns** - Evidence events, workspace snapshots, focus-aware rendering all tested
3. **Incremental adoption** - Can start with just the terminal emulator stack, add other features later

---

## Next Steps (Phase 1 Focus)

### Week 1: Core VTE Terminal Primitive

1. **Day 1-2:** Set up Rust project with `vte` crate dependency
2. **Day 2-4:** Implement `TerminalState` struct (grid, cursor, scrollback)
3. **Day 4-5:** Wire up `AnsiParser` to feed PTY bytes → update state

### Week 2: Render Loop & Input Handling

1. **Day 1-2:** Build `PtyCapture` for continuous byte stream reading
2. **Day 2-4:** Implement render loop (blit grid to parent terminal)
3. **Day 4-5:** Add keystroke forwarding (input → PTY write)

### Week 2 End: Validation Demo

**Success criteria checklist:**
- [ ] Single cell renders live terminal with full colors
- [ ] Cursor position and blink work correctly  
- [ ] vim/htop render properly inside the cell
- [ ] Keystrokes respond in real-time (<50ms latency)
- [ ] Scrollback preserves history with correct line wrapping
- [ ] Resize events propagate to backend and reflow content correctly

**Decision gate:** If all criteria met → proceed to Phase 2 (multi-cell grid). If not → iterate on VTE primitive before scaling.

---

## Questions for FrankenTUI Agent (Implementation Details)

Ask these during Week 1 to refine implementation:

1. **Grid storage:** Does `TerminalState` in `state.rs` use a flat `Vec<Cell>` or `Vec<Vec<Cell>>`? (Performance-critical for multi-cell rendering at 60fps)

2. **Resize propagation:** When a pane's area changes, how does FrankenTUI handle reflow? Does it send SIGWINCH to the PTY child? Is there automatic content reflow in the VTE state machine?

3. **PTY implementation:** Does `PtyCapture` use `portable-pty` or raw `openpty`/`forkpty`? (Portability implications for macOS vs Linux)

4. **Dirty tracking:** Is there a dirty-flag mechanism on `TerminalState` to avoid re-blitting unchanged cells? Or does it always render at fixed FPS?

5. **Tmux integration:** Has anyone implemented `tmux pipe-pane` as a backend in FrankenTUI, or is this new for Charon?

---

## Appendix: Key FrankenTUI Files for Reference

| Component | Source File(s) | Lines | Purpose |
|-----------|----------------|-------|---------|
| `TerminalState` | `ftui-extras/src/terminal/state.rs` | 2.7K | Grid + cursor + scrollback state machine |
| `AnsiParser` | `ftui-extras/src/terminal/parser.rs` | 2.6K | VTE-based escape sequence parser |
| `TerminalEmulator` widget | `ftui-extras/src/terminal/widget.rs` | 1.1K | Renders terminal state to buffer |
| `PtyCapture` | `ftui-extras/src/pty_capture.rs` | 692 | Continuous PTY byte stream reader |
| Pane model | `ftui-layout/src/pane.rs` | 3.4K | Stable IDs + tree layout (Phase 2) |
| Workspace snapshot | `ftui-layout/src/workspace.rs` | - | Focus tracking + generation counter |
| Canvas/Braille | `ftui-extras/src/canvas.rs` | 57K | High-res dithering (Phase 4) |
| Text effects | `ftui-extras/src/text_effects.rs` | 383K | Pulse, fade-in, glitch, etc. (Phase 4) |
| Visual FX demo | `ftui-demo-showcase/src/screens/visual_effects.rs` | - | Example usage of effects library |

---

*Document version: 2.1 (Python integration + resize handling)*
*Last updated: 2026-03-24*
