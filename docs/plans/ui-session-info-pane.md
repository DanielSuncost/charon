# TUI Task: Right-Side Session Info Pane

## What to build

A toggleable narrow vertical pane on the right side of the chat view that shows session context. It floats over the chat content in the upper-right corner when there's sufficient terminal width (≥100 cols). Toggle with **Ctrl+I** (info pane).

## Data source

The backend already sends `session_info` in every refresh payload. Read it from the refresh event:

```typescript
case 'refresh': {
  const p = ev.payload as any
  // ...existing refresh handling...
  if (p?.session_info) S.sessionInfo = p.session_info
}
```

The `session_info` object has this shape:

```typescript
interface SessionInfo {
  tasks: Array<{
    task_id: string
    instruction: string  // what the user asked (truncated)
    summary: string      // fact-based summary of what was done
    tokens_in: number
    tokens_out: number
    tool_calls: number
    turns: number
    ts: number           // epoch timestamp
  }>
  goals: Array<{
    id: string
    title: string        // goal description
    status: string       // active, backlog, proposed, confirmed, completed, failed
    intent_type: string  // user_intent, idea, autonomous
    criteria: string[]   // acceptance criteria (may be empty)
  }>
  user_model: string     // pre-rendered user profile block with ═══ delimiters
  tokens: {
    chat_in: number      // total input tokens for chat this session
    chat_out: number     // total output tokens for chat
    summary_tokens: number      // tokens spent on task summarization (currently 0, reserved)
    goal_inference_tokens: number  // tokens spent on goal extraction
    consolidation_tokens: number   // tokens spent on user model updates
  }
}
```

## Layout

```
┌─────────────────────────────────────┬──────────────────────┐
│                                     │ ┌ Tasks ─────────── ┐│
│          Chat view                  │ │ ✓ edited login.py  ││
│          (existing)                 │ │ ✓ ran pytest       ││
│                                     │ │ ✓ fixed auth bug   ││
│                                     │ │ ○ add rate limit   ││
│                                     │ │                    ││
│                                     │ │ ↑↓ scroll          ││
│                                     │ └────────────────────┘│
│                                     │ chat: 45k↑ 12k↓      │
│                                     │ bg: ~3k consolidation │
├─────────────────────────────────────┴──────────────────────┤
│ status bar                                                  │
├─────────────────────────────────────────────────────────────┤
│ input                                                       │
└─────────────────────────────────────────────────────────────┘
```

## Pane specs

- **Width:** 32 chars fixed, or 28% of terminal width, whichever is smaller
- **Height:** fills from top to ~4 lines above the input bar
- **Position:** absolute, right-aligned, floats over chat content
- **Background:** semi-transparent dark (`RGBA.fromValues(0.04, 0.04, 0.07, 0.85)`) — OpenTUI supports RGBA alpha
- **Border:** left border only, thin line in `#3b3252`
- **Only visible when:** terminal width ≥ 100 cols AND pane is toggled on

## Three tabs

Switch tabs with **Tab** key when the pane is focused (Ctrl+I focuses it, Ctrl+I again hides it).

### Tab 1: Tasks (default)

Show `session_info.tasks` newest first. Each task has:
- `short`: pre-truncated label (~25 chars, for display)
- `summary`: full one-liner
- `detail`: multi-line description (shown on Enter)

```
Tasks (12)
────────────────────────────
✓ edited auth/login.py; ...
✓ read README.md, docs/p...
✓ wrote tests/test_auth.py
○ searched "auth best pr...
────────────────────────────
▶ Press Enter for details
```

Icons: `✓` completed, `✗` failed, `○` in progress
Highlighted row shown with `▶` prefix and brighter color.
Scrollable with ↑↓ arrows when pane is focused.

**On Enter:** send `{type: 'task_detail', task_id: '...'}` to backend.
Backend responds with `{type: 'task_detail', detail: '...'}`.
Show the detail as a temporary overlay or push it as a chat message:

```
╭─ Task Detail ──────────────╮
│ Task: fix the auth bug     │
│ Result: edited auth/logi...│
│ Tools: 3 calls, 2 turns   │
│ Tokens: 1234↑ 567↓        │
╰────────────────────────────╯
```

### Tab 2: Goals

Show `session_info.goals`:

```
Goals (5)
──────────────────────
● Build auth system
  [active] 2 criteria
○ Add rate limiting
  [backlog]
◆ Improve test coverage
  [proposed] /confirm
✓ Setup project
  [completed]
```

Icons: `●` active, `○` backlog, `◆` proposed, `✓` completed, `✗` failed
Show acceptance criteria count if any.
For proposed goals, show `/confirm` hint.

### Tab 3: User Model

Show `session_info.user_model` (pre-rendered text with ═══ delimiters):

```
User Model
──────────────────────
Style: concise, direct
Coding: snake_case,
  explicit exceptions
Tooling: Python 3.12,
  uv, ruff, bun
Corrections:
- Never bare except
- X | None not Optional
```

Just render the text, word-wrapped to pane width. Scrollable.

## Token footer (always visible at bottom of pane)

```
──────────────────────
chat: 45.2k↑ 12.1k↓
bg: ~3k consol
```

Format large numbers with k/M suffix. Show consolidation tokens if > 0.

## Tab indicator

At the top of the pane, show which tab is active:

```
[Tasks] Goals  Model
```

Active tab is bold/highlighted. Others are dim. Click or Tab to switch.

## Keybindings

- **Ctrl+I** — toggle pane visibility (and focus)
- **Tab** (when pane focused) — cycle tabs: Tasks → Goals → Model
- **↑↓** (when pane focused) — scroll content
- **Escape** (when pane focused) — return focus to chat input

## Implementation notes

- The pane is an OpenTUI `Box` with `position: 'absolute'`, `right: 0`, `top: 0`
- Use `instantiate(renderer, Box({...}))` like the existing dashboard/sessions
- Content is a `Text` renderable updated on each refresh
- Hide when terminal width < 100: check `process.stdout.columns` on resize
- The pane state lives in `S`: `S.infoPaneOpen`, `S.infoPaneTab`, `S.infoPaneScroll`
- Data comes from `S.sessionInfo` (set from refresh payload)
- Don't re-render on every keystroke — only on refresh events and tab/scroll changes

## Files to modify

- `apps/tui/opentui/src/index.ts` — add the pane renderable, keybindings, state
- No backend changes needed — `session_info` is already in the refresh payload
