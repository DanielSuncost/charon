# Session Summarizer — Background Agent Observer

> Generates brief running summaries of what each agent session is doing.

## Purpose

Each session cell in the Session Grid shows a one-line summary above it
in gold (active), grayblue (idle), or bright blue (waiting) text. This
summary needs to be generated and kept current automatically.

## How It Works

A background process (or thread in the chat_backend) periodically:

1. Captures the last N lines of each active tmux session
2. Sends them to the configured LLM with a prompt like:
   "Summarize what this coding agent is currently doing in ≤10 words"
3. Caches the result (don't re-summarize if the screen hasn't changed)
4. Detects session state:
   - **active**: screen content changed recently (last 5s)
   - **idle**: screen hasn't changed for >30s
   - **waiting**: screen contains a prompt/question awaiting input
     (detect by patterns like `?`, `[y/n]`, `confirm`, `approve`)

## Data Flow

```
tmux capture (every 1s)
  → hash screen content
  → if changed since last summary:
      → send last 20 lines to LLM
      → get ≤10 word summary
      → store in summary cache
  → detect state (active/idle/waiting)
  → frontend polls summary cache via refresh payload
```

## Implementation

### Phase 1: Heuristic summaries (no LLM needed)

- Parse the last line of tmux output for common patterns:
  - `$` or `❯` at end → idle at prompt
  - `running...` or spinner → actively working
  - `?` or `[y/n]` → waiting for input
  - File path + line number → editing a file
  - `Error` or `FAIL` → error state
- Display the detected state as the summary line

### Phase 2: LLM-powered summaries (requires provider)

- Use a small/fast model (or the configured model)
- Very short prompt, ≤10 word response
- Cache aggressively — only re-summarize when screen changes
- Rate limit: max 1 summary per session per 10 seconds

### Phase 3: Analytics

- Track token usage per session over time
- Track activity patterns (when active, how long idle)
- Store metrics in SQLite for historical analysis
- Display sparklines in the dashboard

## Session States

| State | Border Color | Summary Color | Detection |
|-------|-------------|---------------|-----------|
| active | gold #d4a44a | gold #d4a44a | Screen changed in last 5s |
| idle | grayblue #6b7f99 | grayblue #6b7f99 | No change for 30s+ |
| waiting | bright blue #60a5fa | bright blue #60a5fa | Prompt/question detected |
| stopped | dim red #6b3333 | dim red #6b3333 | tmux session not found |
| error | red #ef4444 | red #ef4444 | Error pattern in output |
