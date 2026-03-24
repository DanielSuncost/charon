# User Model Schema & Consolidation Process

> Structured schema for Charon's persistent user model, the background
> consolidation process that maintains it, and how it renders into the
> system prompt.
>
> Created: 2026-03-21
> Status: Design (ready to implement)
> Related: agent-memory-goals-usermodel-design.md, three-tier-memory.md

---

## 1. Schema

The user model has seven categories. Each serves a distinct purpose in
shaping agent behavior.

### 1.1 Communication Style

How the user wants to be talked to. Affects every response.

```yaml
style:
  verbosity: concise          # concise | detailed | adaptive
  technical_level: expert     # beginner | intermediate | expert
  tone: direct                # formal | casual | direct
  explanation_preference: show_code  # explain_first | show_code | both
```

### 1.2 Coding Conventions

How the user writes code. Global defaults; projects can override.

```yaml
coding:
  naming: snake_case
  error_handling: "explicit exceptions, never bare except"
  type_hints: "X | None not Optional[X]"
  testing: "alongside implementation, pytest"
  imports: "explicit, no star imports"
  comments: "only for non-obvious logic"
```

### 1.3 Tooling & Environment

What the user has installed and prefers.

```yaml
tooling:
  python: "3.12+, uv for deps, ruff for linting"
  javascript: "bun, not npm"
  editor: "terminal-based (pi, hermes, charon)"
  os: linux
  gpu: "5090, 32GB VRAM"
  local_models: "LM Studio, qwen3-30b-a3b"
```

### 1.4 Workflow Preferences

How the user structures work.

```yaml
workflow:
  pr_size: "small and focused"
  review_process: thorough
  branching: "feature branches, rebase before merge"
  testing_before_commit: always
  documentation: "inline + README updates"
```

### 1.5 Corrections

Highest-signal entries. Things the user explicitly corrected. These are
**never deleted** by consolidation — only the user can remove them.

```yaml
corrections:
  - "Never use bare except — catch specific exceptions"
  - "Use X | None not Optional[X]"
  - "Don't explain what you're about to do, just do it"
  - "Don't ask for confirmation on file edits, just make them"
```

### 1.6 Cross-Project Intentions

What the user is trying to accomplish across their portfolio. Gives
agents and the overseer strategic context.

```yaml
intentions:
  - project: charon
    intent: "Build into a working multi-agent OS, ship V1"
    priority: high
    last_updated: 2026-03-20
  - project: hermes
    intent: "Contributor, occasional fixes"
    priority: low
  - project: pi-mono
    intent: "Reference implementation, study"
    priority: low
```

### 1.7 Interaction Patterns

How the user actually uses Charon. Learned by observation, not stated
explicitly. Helps agents predict behavior and adapt.

```yaml
patterns:
  steers_frequently: true
  prefers_autonomous_work: true
  reviews_before_commit: true
  active_hours: "9am-11pm UTC-5"
  session_length: long            # short (<30m) | medium | long (>2h)
  idea_capture_frequency: high
```

---

## 2. Storage

### Primary: SQLite `user_model` table

Stored as key-value pairs where the key is the category name and the
value is a JSON object:

```sql
-- Existing table, new structure in values
INSERT INTO user_model (key, value) VALUES
  ('style', '{"verbosity": "concise", "technical_level": "expert", ...}'),
  ('coding', '{"naming": "snake_case", "error_handling": "explicit...", ...}'),
  ('corrections', '["Never bare except", "X | None not Optional[X]", ...]'),
  ('intentions', '[{"project": "charon", "intent": "...", ...}]'),
  ('patterns', '{"steers_frequently": true, ...}'),
  ('_meta', '{"last_consolidated_at": "...", "last_consolidated_message_count": 42}');
```

### Human-readable: `.charon_state/USER.md`

```markdown
# User Profile

## Style
concise, expert, direct, show code

## Coding
snake_case, explicit exceptions, X | None, pytest alongside

## Tooling
Python 3.12/uv/ruff, bun, Linux, 5090 GPU, LM Studio

## Workflow
small PRs, thorough review, test before commit

## Corrections
- Never bare except
- X | None not Optional[X]
- Don't explain, just do it

## Intentions
- charon (high): Build into a working multi-agent OS, ship V1
- hermes (low): Contributor, occasional fixes

## Patterns
steers often, prefers autonomy, long sessions, active 9am-11pm UTC-5
```

### Backward compatible

The `UserModel` tool continues to work with flat entries for agents that
haven't been updated to use the structured format. The consolidation
process migrates flat entries into the structured categories.

---

## 3. System Prompt Rendering

The user model renders into the system prompt as a delimited block:

```
══════════════════════════════════════════════
USER PROFILE [42% — 840/2,000 chars]
══════════════════════════════════════════════
Style: concise, expert, direct, show code
Coding: snake_case, explicit exceptions, X | None, pytest alongside
Tooling: Python 3.12/uv/ruff, bun, Linux, 5090 GPU, LM Studio
Workflow: small PRs, thorough review, test before commit
Corrections:
- Never bare except
- X | None not Optional[X]
- Don't explain, just do it
Intentions: charon (high, shipping V1), hermes (low, occasional)
Patterns: steers often, prefers autonomy, long sessions
══════════════════════════════════════════════
```

The opening and closing `═══` delimiters clearly mark the block
boundaries so the LLM doesn't confuse profile entries with instructions
or conversation content. The usage indicator (`42% — 840/2,000 chars`)
lets the agent know how much budget remains.

Empty categories are omitted. A brand new user sees only:

```
══════════════════════════════════════════════
USER PROFILE [0% — 0/2,000 chars]
══════════════════════════════════════════════
(No profile yet. Save preferences with the UserModel tool.)
══════════════════════════════════════════════
```

---

## 4. Consolidation Process

### What it does

A background task that periodically reviews recent agent interactions
and updates the structured user model:

1. **Extract signals** from recent interactions:
   - User corrections → add to `corrections` (verbatim)
   - User preferences expressed in chat → update `style`, `coding`,
     `workflow` categories
   - Steering patterns → update `patterns`
   - Project focus → update `intentions`

2. **Merge redundant entries** — if multiple agents noted the same
   preference, consolidate into one structured entry

3. **Infer patterns** — analyze interaction history:
   - Steering frequency → `steers_frequently`
   - Session durations → `session_length`
   - Active hours → `active_hours`
   - Idea capture rate → `idea_capture_frequency`

4. **Update cross-project intentions** — based on which projects are
   getting the most attention and what goals are active

5. **Prune stale observations** — if a pattern entry hasn't been
   reinforced in 30+ days, flag it as stale (but don't delete)

### What it never does

- **Never deletes corrections.** Corrections are sacred — the user
  explicitly said "do it this way." Only the user can remove them
  (via the UserModel tool or editing USER.md).
- **Never runs during a task.** The frozen snapshot pattern means the
  consolidation writes to the backing store, but running tasks see
  the snapshot from their start time.
- **Never changes entries the user manually edited in USER.md.** If the
  user edited the file directly, those edits are the ground truth.

### When it runs

**Trigger rule:** The consolidation runs only when there is fresh user
signal to process. Specifically:

- At least **1 new user-origin event** (chat message, steer, follow-up,
  `/idea`, `/command`) since `last_consolidated_at`, OR
- At least **3 new task completions** since `last_consolidated_at`
  (completed tasks may contain implicit signals from steering)

If neither condition is met, the consolidation is skipped. **Zero cost
when the user is away.** Agents running overnight on timed sessions do
not trigger consolidation — it waits until the user comes back and
interacts.

**Check frequency:** Every 50 heartbeats (~100 minutes when the daemon
is running). Also triggered after every 10th user-origin event.

**Model used:** The fast/cheap model tier. Consolidation is a
summarization task, not a complex reasoning task.

### Metadata

The consolidation stores its state in the user model:

```json
{
  "_meta": {
    "last_consolidated_at": "2026-03-21T14:30:00Z",
    "last_consolidated_message_count": 42,
    "last_consolidated_task_count": 15,
    "consolidation_count": 7,
    "schema_version": "2.0"
  }
}
```

---

## 5. How Agents Write to the User Model

### During conversation (UserModel tool)

Agents use the `UserModel` tool to write entries. The tool accepts both
flat entries (backward compatible) and structured updates:

```
# Flat entry (current behavior, still works)
UserModel(action='add', content='Prefers snake_case naming')

# Structured update (new)
UserModel(action='set', category='coding', key='naming', value='snake_case')

# Correction (always goes to corrections list)
UserModel(action='correct', content='Never use bare except')
```

The `correct` action is a shortcut that always appends to the
`corrections` list, even if similar wording exists elsewhere. Corrections
are the user's explicit voice and get priority rendering.

### During consolidation (background)

The consolidation process has write access to all categories. It reads
from:
- Agent inbox events (task_received, task_succeeded, task_failed)
- Steering messages (user redirections mid-task)
- Follow-up messages (queued instructions)
- Idea captures (`/idea` entries)
- Goal completions and status changes

---

## 6. What This Improves

| Before (flat entries) | After (structured model) |
|----------------------|-------------------------|
| "Prefers concise responses" and "user likes brief answers" coexist | One `style.verbosity: concise` entry |
| No distinction between stated preferences and observed patterns | Corrections (stated) vs patterns (observed) clearly separated |
| No cross-project awareness | `intentions` section shows project portfolio and priorities |
| Agents can't reason about preference *type* | Categories let agents know "this is about coding style" vs "this is about workflow" |
| Every agent independently discovers the same preferences | Consolidation merges multi-agent observations into one model |
| No staleness detection | Consolidation flags unreinforced patterns |
| User can't easily review what agents know | USER.md is a clean markdown file with sections |

---

## 7. Implementation Plan

1. **Extend UserModel tool** with `set` and `correct` actions for
   structured writes. Keep `add`/`replace`/`remove` for flat entries
   (backward compat).

2. **Update `_build_user_model` in system_prompt_builder** to render
   the structured format with `═══` delimiters.

3. **Add consolidation task type** to the daemon loop. Registered as a
   recurring task like the overseer's monitoring cycle.

4. **Consolidation logic** — reads recent events, calls fast model to
   extract/merge signals, writes to structured categories.

5. **Migration** — on first consolidation run, migrate any existing flat
   entries into the appropriate structured categories.

6. **USER.md sync** — after every write (tool or consolidation), export
   the structured model to USER.md in readable markdown format.
