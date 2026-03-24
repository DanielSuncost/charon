# Charon UI Workstream — Current Focus

> For the agent working on the TUI, charons-boat, session connectivity,
> and visual polish.

## What's Built

### TUI (`apps/tui/opentui/`)
- Three views: Chat (F1), Dashboard (F2), Sessions Grid (F3)
- Responsive mascot with full/mid/tiny variants, dynamic resize
- Slash command menu (vertical, navigable, `/setup` flow)
- Chat with streaming responses, tool call display, thinking animation
- Dashboard with agents/projects/rearview (real multi-column Box layout)
- Session Grid with agent filtering, project filtering, visibility toggles
- Live tmux capture in grid cells (150ms poll for active, 1s for background)
- Enter to interact with tmux sessions, z to zoom, Escape to exit
- Status-colored borders (gold/grayblue/blue) with heuristic state detection
- Summary lines above each session cell

### charons-boat (`tools/charons-boat/`)
- CLI tool: wrap, serve, register, status, stop, revoke
- Pairing code generation
- Session registration in ~/.charon/boats/

### Backend (`apps/tui/opentui/chat_backend.py`)
- ConversationEngine integration (direct, no daemon polling)
- Agent/session/project discovery (Charon agents + process scan + tmux)
- tmux capture/send protocol
- Heuristic session state detection
- /setup command handling with OAuth support

### Infrastructure (`apps/core-daemon/`)
- `tmux_capture.py` — local and remote tmux capture/send
- `provider_bridge.py` — onboarding config → provider/model
- `process_inspector.py` — detect running agent processes

## Remaining Work

### Immediate
1. Polish session grid cell rendering (text overflow, sizing edge cases)
2. Make `charon` command the entry point (launches TUI directly)
3. Wire onboarding completion to auto-create agent + start daemon

### charons-boat Remote
4. Build SSH tunnel manager in tmux_capture.py
5. Implement `/link user@host --code CODE` command
6. Remote session discovery over SSH
7. Remote tmux capture/send over SSH tunnel
8. Milestone: interact with remote pi-agent from local Charon

### Session Summarizer
9. Background heuristic summaries (Phase 1 — pattern matching)
10. LLM-powered summaries (Phase 2 — requires provider)
11. Activity analytics and token tracking (Phase 3)

### Visual Polish
12. Better dashboard column layout (real side-by-side when possible)
13. Token usage meters and sparklines
14. Theme system
15. Notification indicators

## OpenTUI Lessons Learned

Critical knowledge for anyone working on the TUI:

1. **Factory functions return VNode proxies** — `Text()`, `Box()`, `Input()` etc.
   return proxies, NOT real renderables. Setting `.content` on a proxy queues
   the change but never applies it. Use `instantiate(renderer, Text({...}))` for
   any renderable you need to update dynamically.

2. **StyledText can't be interpolated in `t` templates** — `t\`\${styledText}\``
   produces `[object Object]`. Concatenate chunk arrays manually:
   `new SC([...a.chunks, ...b.chunks])` via the `joinStyled()` helper.

3. **`display = false` doesn't hide elements** — nor does `height = 0`.
   View switching works by swapping content on a single Text renderable,
   or by `root.add()` / `root.remove()` of Box trees.

4. **`onKeyDown` and `InputRenderableEvents` don't fire** — Only
   `renderer.keyInput.on('keypress')` works reliably for key handling.
   Keys have `.name` like `'f1'`, `'f2'`, `'return'`, `'tab'`, `'up'`, etc.

5. **Async content changes need `renderer.requestRender()`** — Changes from
   backend event callbacks don't trigger redraws automatically.

6. **`input.blur()` may not work** — To prevent Input from eating keys,
   handle entered-session key forwarding at the TOP of the keypress handler
   with `key.preventDefault()`.
