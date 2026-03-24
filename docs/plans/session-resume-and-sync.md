# Session Resume & Multi-Tab Sync Plan

## Problems Identified

### 1. Auto-resume is wrong
Currently `charon` auto-resumes the last conversation. This causes issues:
- Opening charon in two tabs resumes the same session in both
- No way to start fresh without `--force` flags
- User should explicitly choose to resume

### 2. No cross-tab sync for Charon agent sessions
Charon agents run inside the chat_backend.py process, not in tmux. So:
- Two tabs running the same agent have separate engine instances
- Messages sent in one tab don't appear in the other
- The session grid can't "capture" a Charon agent's output like it can tmux

## Fixes

### Fix 1: Remove auto-resume, add /resume command
- **Remove** auto-resume on startup
- **Keep** conversation persistence (save on every exchange + exit)
- **Add** `/resume` command that shows recent sessions in a popup:
  ```
  ╭─ Recent Sessions ────────────────────────────╮
  │ ▸ AG-0001  charon-main  12 msgs  2 min ago   │
  │   AG-0002  charon-dev   45 msgs  1 hour ago   │
  │   AG-0003  charon-infra  8 msgs  3 hours ago  │
  │                                                │
  │   (view all)                                   │
  ╰─ ↑↓ navigate  Enter resume  Esc cancel ──────╯
  ```
- **Add** `/resume <agent-id>` for direct resume
- **Keep** `charon --resume=AG-0001` CLI flag for scripting
- **New session by default** — `charon` always starts fresh

### Fix 2: Ctrl+C behavior
- **First Ctrl+C**: clear input box text (if any text present)
- **Second Ctrl+C** (within 2 seconds, or if input was already empty): exit
- Matches terminal convention (Ctrl+C = cancel current input)

### Fix 3: Charon agent session architecture (bigger)
The fundamental issue: Charon agents aren't in tmux, they're in-process.

**Short term (do now):**
- Virtual sessions in the grid (already implemented) — show chat history
- Entering a virtual session switches to chat view
- Accept that two tabs = two separate instances (document this)

**Medium term:**
- Run each Charon agent's engine as a background daemon process
- Use Unix socket or SQLite WAL for IPC between TUI and daemon
- Multiple TUI instances connect to the same daemon
- Session grid shows live state from the daemon

**Long term (charons-boat):**
- Charon agents run in managed tmux sessions
- The conversation engine runs inside tmux
- TUI captures/sends to tmux like any other session
- Full multi-tab sync for free (tmux handles it)
- This is the same architecture as remote sessions

## Implementation Order
1. Ctrl+C double-tap (quick win)
2. Remove auto-resume, add /resume command
3. Document single-tab-per-agent limitation
4. Plan daemon architecture for medium-term fix
