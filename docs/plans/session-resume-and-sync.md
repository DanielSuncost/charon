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

---

## Resume Bug Post-Mortem (2026-04-09 → 2026-04-12)

### What users saw
- Resume showed a summary instead of full conversation context
- Sessions disappeared from the resume menu
- New messages sent after resume weren't visible on the next resume
- `/resume <session-id>` returned "No saved conversation" for sessions that existed

### Root causes (three bugs, one design flaw)

**Design flaw: Dual persistence with no coordination**

Charon has two storage backends — JSONL files (legacy) and SQLite via the
lossless context store. Resume always read from JSONL. Compaction destroyed
messages in the JSONL. The lossless store preserved everything but was never
consulted during resume.

```
Session lifecycle (broken):

  message → engine.messages → _persist_message(SQLite) ✓
                            → save_conversation(JSONL overwrite) ✓
  
  compact → engine.messages = [summary + 4 msgs]  ← destructive
          → save_conversation(JSONL overwrite)     ← writes compacted data
  
  resume  → load_conversation(JSONL)               ← gets compacted data
          → ??? SQLite has full history but nobody asks
```

**Bug 1: JSONL overwritten with compacted messages**

`save_conversation()` overwrites the entire JSONL file with `engine.messages`
after every turn. After legacy compaction, `engine.messages` only contains a
summary + last 4 messages. The full history was destroyed on disk.

Fix: Save paths now write from SQLite (full history) when the lossless store
is active, falling back to `engine.messages` only when SQLite is unavailable.

**Bug 2: Sessions with timestamp=0 sorted to bottom of resume menu**

When saving messages reconstructed from SQLite's `StoredMessage`, the
`created_at` ISO timestamp wasn't converted to epoch seconds. Messages got
`timestamp=0.0`, and the resume menu (sorted by timestamp descending) pushed
them to position 191 of 191.

Fix: Added `_iso_to_epoch()` conversion. Also added file-mtime fallback in
`list_conversations()` so sessions with bad timestamps still sort correctly.

**Bug 3: `/resume` reused existing engine with wrong agent_id**

`/resume <session-id>` called `_ensure_engine()` which returned the
*existing* engine (created for the current session). The engine's `agent_id`
didn't match the target session, so:
- `load_from_store()` queried SQLite with the wrong agent_id → 0 results
- JSONL fallback found no file → "No saved conversation"
- New messages after resume were persisted under the wrong agent_id

Fix: `/resume` now sets `self.engine = None` to force re-creation with the
correct agent_id. Also queries SQLite directly with the target agent_id
via `_full_messages_from_store(arg)` instead of going through the engine.

**Bug 4: Sessions only in SQLite didn't appear in resume menu**

`list_conversations()` only scanned JSONL files. Sessions where the terminal
was killed (no clean exit → no JSONL written) existed only in SQLite and were
invisible to the resume menu.

Fix: `list_conversations()` now merges sessions from SQLite that have no
corresponding JSONL file.

### Correct lifecycle after fixes

```
Session lifecycle (fixed):

  message → engine.messages
          → _persist_message(SQLite)          ← source of truth
          → save_conversation(JSONL from SQLite full history)  ← backup

  compact → context_window table updated      ← what LLM sees
          → conversation_messages untouched   ← raw history preserved

  resume  → _full_messages_from_store(SQLite) ← full context restored
          → JSONL fallback only if SQLite empty
          → engine re-created with correct agent_id

  list    → JSONL scan + SQLite merge         ← nothing invisible
```

### Lesson
Never have two persistence backends where one is the write path and the
other is the read path.  The lossless SQLite store should have been wired
into resume from the start — it was designed exactly for this purpose but
only connected to compaction, not to restoration.
