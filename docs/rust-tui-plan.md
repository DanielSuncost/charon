# Rust TUI Migration Plan for Charon

**Status:** Proposed | **Date:** 2026-03-23 | **Trigger:** Session-grid pain point

---

## Executive Summary

Adopt a **stripped-down FrankenTUI-inspired architecture** to solve the session-grid UX problem, with incremental expansion toward advanced features. This cleanroom approach validates core abstractions before committing to full Rust TUI rewrite.

---

## Phase 1: Core Pane Model (Weeks 1-2)

### Goal
Enable tabbed session switching with preserved state and live updates.

### Components to Adapt

#### 1.1 `PaneId` + `PaneTree` Data Structures
**Source:** `frankentui/crates/ftui-layout/src/pane.rs`

```rust
// Each session gets a stable identifier
pub struct PaneId(u64);

// Tree structure for nested layouts (future-proofing)
pub enum PaneNodeKind {
    Leaf(PaneId),           // Single session
    Split {                 // Multi-pane layout
        axis: SplitAxis,
        children: Vec<PaneNode>,
        ratio: f32,
    },
}
```

**Why:** Stable IDs prevent re-rendering entire UI on tab switch. Each session maintains its own scrollback and chat state independently.

#### 1.2 `WorkspaceSnapshot` with Active Pane Tracking
**Source:** `frankentui/crates/ftui-layout/src/workspace.rs`

```rust
pub struct WorkspaceSnapshot {
    pub pane_tree: PaneTree,
    pub active_pane_id: Option<PaneId>,
    pub generation: u64,     // For determinism/replay
}
```

**Why:** Tab switching becomes a single field change (`active_pane_id`) instead of full state reconstruction. The `generation` counter enables deterministic replay later if needed.

#### 1.3 Basic Render Loop with Focus-Aware Optimization
**Source:** `frankentui/crates/ftui-runtime/src/render_trace.rs`

```rust
// Only fully render the active pane; others update in background
pub fn render_frame(workspace: &WorkspaceSnapshot, area: Rect) -> Frame {
    let mut frame = Frame::new(area);
    
    for pane_id in workspace.pane_tree.all_panes() {
        if Some(pane_id) == workspace.active_pane_id {
            // Full fidelity render with styles, animations
            render_session_full(&sessions[pane_id], &mut frame);
        } else {
            // Minimal render or skip (cached state preserved)
            render_session_minimal(&sessions[pane_id], &mut frame);
        }
    }
    
    frame
}
```

**Why:** Reduces CPU/GPU load when inactive sessions don't need full rendering. Critical for smooth tab switching.

### Success Criteria
- [ ] Can switch between 3+ sessions with <100ms latency
- [ ] Each session preserves scrollback position on switch
- [ ] Inactive sessions continue receiving live updates (just not fully rendered)
- [ ] No data loss or state corruption during rapid tab switching

---

## Phase 2: Live Updates & Scrollback (Weeks 3-4)

### Goal
Make inactive sessions feel "alive" with real-time updates visible on switch.

### Components to Adapt

#### 2.1 Per-Pane Scrollback Buffer
**Source:** `frankentui/crates/ftui-widgets/src/virtualized.rs` (simplified)

```rust
pub struct SessionState {
    pub scrollback: VecDeque<String>,  // Or Line objects for styling
    pub viewport_offset: usize,        // Current visible position
    pub total_lines: u64,              // For scrollbar calculation
}
```

**Why:** Each session maintains independent history. Switching tabs shows exactly where you left off.

#### 2.2 Subscription-Based Message Routing
**Source:** `frankentui/crates/ftui-runtime/src/subscription.rs`

```rust
// All sessions subscribe to their respective agent message streams
pub struct SessionSubscription {
    pub pane_id: PaneId,
    pub message_stream: Receiver<AgentMessage>,
}

// Runtime delivers messages to all subscriptions; render loop picks active one
```

**Why:** Decouples message delivery from rendering. Sessions accumulate updates even when not visible.

#### 2.3 Scrollbar Widget (Optional but Recommended)
**Source:** `frankentui/crates/ftui-widgets/src/scrollbar.rs`

Simple vertical scrollbar showing position in scrollback buffer.

### Success Criteria
- [ ] Switching tabs shows accumulated messages from inactive period
- [ ] Scrollbar accurately reflects position in history
- [ ] No lag when returning to session after 30+ seconds of inactivity
- [ ] Memory usage bounded per-session (no unbounded growth)

---

## Phase 3: Remote Session Support (Month 2)

### Goal
Monitor and interact with sessions running on remote servers as if local.

### Components to Adapt

#### 3.1 PTY Capture for Remote Sessions
**Source:** `frankentui/crates/ftui-harness/src/pty_capture.rs` + `crates/ftui-extras/src/pty_capture.rs`

```rust
pub struct PtyCapture {
    // Wraps portable-pty for subprocess output capture
}

// For remote sessions, replace PTY with network stream
pub enum SessionBackend {
    LocalPty(PtyCapture),
    RemoteStream(TcpStream),  // Or WebSocket for web adapter
}
```

**Why:** Same abstraction works for local terminal processes and remote TCP/WebSocket streams. Enables seamless switching between local and remote sessions.

#### 3.2 Evidence Events for Deterministic Replay
**Source:** `frankentui/crates/ftui-runtime/src/render_trace.rs`

```rust
pub enum EvidenceEvent {
    MessageReceived { pane_id: PaneId, timestamp: Instant },
    UserInput { pane_id: PaneId, key: KeyCode },
    ScrollPositionChanged { pane_id: PaneId, offset: usize },
}

// Events logged to JSONL for later replay/analysis
```

**Why:** Remote sessions can be audited and debugged. If a session misbehaves, you can replay its event log to understand what happened.

#### 3.3 Connection State Indicator
Visual indicator showing remote session status (connected, disconnected, reconnecting).

### Success Criteria
- [ ] Can open session connected to remote server via TCP/WebSocket
- [ ] Session survives network blips with auto-reconnect
- [ ] Visual indicator shows connection state clearly
- [ ] Switching between local and remote sessions feels identical

---

## Phase 4: Visual Polish & Effects (Month 2+)

### Goal
Add "delight" features that make the TUI feel premium.

### Components to Adapt

#### 4.1 Canvas with Braille Dithering
**Source:** `frankentui/crates/ftui-extras/src/canvas.rs`

```rust
pub enum Mode {
    Block,      // Standard terminal cells
    Braille,    // High-res dithering using Braille dots (2x4 grid per cell)
}

// For the mascot's lantern:
let mut painter = Painter::new(10, 10, Mode::Braille);
painter.fill_circle(cx, cy, radius, color);
```

**Why:** Braille mode gives 8x resolution within a single terminal cell. Perfect for smooth gradients and shimmer effects on small elements like the mascot's lantern.

#### 4.2 Text Effects Library
**Source:** `frankentui/crates/ftui-extras/src/text_effects.rs`

Key effects to adapt:

| Effect | Use Case in Charon |
|--------|-------------------|
| `Pulse` | Mascot's lantern shimmer, notification badges |
| `FadeIn` | New message appearance, smooth transitions |
| `RainbowGradient` | Fun mode for agent names or headers |
| `Glitch` | Error states, disconnection warnings |

```rust
// Lantern shimmer example:
let lantern = StyledText::new("🏮")
    .effect(TextEffect::Pulse { 
        speed: 2.0, 
        min_alpha: 0.3 
    })
    .base_color(PackedRgba::rgb(255, 180, 50))  // Orange-gold
    .time(current_time);
```

**Why:** Adds personality and visual feedback without heavy computation. Pulse effect is perfect for the lantern shimmer you mentioned.

#### 4.3 Inline Mode (Optional High-Impact Feature)
**Source:** `frankentui/crates/ftui-demo-showcase/src/screens/inline_mode.rs`

```rust
pub enum DisplayMode {
    Inline,     // UI at bottom, logs scroll above
    AltScreen,  // Full-screen dedicated mode
}
```

**Why:** Lets you preserve command history while keeping the chat interface pinned. Great for power users who want to see tool output alongside agent responses.

### Success Criteria
- [ ] Mascot lantern has smooth shimmer effect (no stutter)
- [ ] New messages fade in smoothly
- [ ] Connection state changes have visual feedback
- [ ] Braille dithering renders cleanly on supported terminals

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────┐
│                    Charon TUI                       │
├─────────────────────────────────────────────────────┤
│  WorkspaceSnapshot                                  │
│  ├─ PaneTree (session layout)                      │
│  └─ active_pane_id (focused session)               │
├─────────────────────────────────────────────────────┤
│  Session Manager                                    │
│  ├─ SessionState[PaneId]                           │
│  │   ├─ scrollback buffer                          │
│  │   ├─ viewport offset                            │
│  │   └─ backend (LocalPty | RemoteStream)          │
│  └─ Subscription routing per session               │
├─────────────────────────────────────────────────────┤
│  Render Loop                                        │
│  ├─ Focus-aware rendering (full vs minimal)        │
│  ├─ Canvas painter (Braille mode for effects)      │
│  └─ Text effects engine                            │
├─────────────────────────────────────────────────────┤
│  Event Handler                                      │
│  ├─ Tab switching (active_pane_id updates)         │
│  ├─ Message delivery to all sessions               │
│  └─ Scroll/viewport management                     │
└─────────────────────────────────────────────────────┘
```

---

## Risk Assessment

### Technical Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Rust TUI ecosystem less mature than Python | Medium | FrankenTUI proven; can borrow patterns |
| Braille dithering not supported on all terminals | Low | Fallback to block mode; document requirement |
| Remote session latency feels different from local | Medium | Add connection indicator; tune reconnection logic |

### Scope Creep Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Building too much before validating pane model | High | **Stick to Phase 1 first**; don't add effects until sessions work |
| Over-engineering remote session support early | Medium | Defer to Phase 3; focus on local UX first |

---

## Decision Log

### Why Stripped-Down vs. Full FrankenTUI?

**Decision:** Start minimal, expand incrementally.

**Rationale:**
1. **Validate core abstraction first** - If pane model doesn't solve session-grid pain point, no point adding visual effects
2. **Lower initial complexity** - Don't need Mermaid diagrams or complex forms on day one
3. **Cleanroom compatibility** - Easier to integrate with existing Python backend initially; can migrate fully later

### Why Not Just Use Existing TUI Framework?

**Decision:** FrankenTUI-inspired custom implementation.

**Rationale:**
1. **Pane model is unique** - Most TUI frameworks don't have stable-pane-ID + workspace-snapshot pattern
2. **Proven for multi-session use cases** - Designed for exactly this problem (terminal multiplexing with focus)
3. **Deterministic by design** - Evidence events and generation counters enable replay/debugging later

---

## Next Steps

1. **Week 1:** Implement `PaneId` + `WorkspaceSnapshot` data structures in Rust
2. **Week 1-2:** Build basic tab switching with preserved scrollback
3. **Week 3:** Add live message delivery to all sessions
4. **Week 4:** Polish render loop with focus-aware optimization
5. **Review point:** Demo Phase 1 complete; decide on Phase 2 scope

---

## Appendix: Key FrankenTUI Files for Reference

| Component | Source File(s) |
|-----------|----------------|
| Pane model | `frankentui/crates/ftui-layout/src/pane.rs` (3.4K lines) |
| Workspace snapshot | `frankentui/crates/ftui-layout/src/workspace.rs` |
| PTY capture | `frankentui/crates/ftui-extras/src/pty_capture.rs` |
| Canvas/Braille | `frankentui/crates/ftui-extras/src/canvas.rs` (57K lines) |
| Text effects | `frankentui/crates/ftui-extras/src/text_effects.rs` (383K lines) |
| Visual FX demo | `frankentui/crates/ftui-demo-showcase/src/screens/visual_effects.rs` |

---

*Document version: 1.0*
*Last updated: 2026-03-23*
