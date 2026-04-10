# Claude Code User MVP

## Goal

Add a Charon-managed "Claude Code user" capability that reuses the existing rooms/groups model.

Charon should be able to:
- launch or attach to a Claude Code session
- treat that session as an external coding participant in a room
- have a Charon operator agent drive Claude Code on the user's behalf
- judge/review Claude Code output using existing review/checkpoint concepts
- persist room events and operation state instead of relying on terminal text alone

## Product model

### Core roles
- **Coordinator / room owner**: optional top-level orchestrator
- **Claude Code User agent**: a Charon agent that instructs and supervises Claude Code
- **Claude Code external participant**: attached tmux/boat/pty session
- **Judge**: reviews Claude Code output and requests repair or acceptance
- **Verifier**: optional later, checks integration/runtime evidence

### Canonical loop
1. User asks Charon to use Claude Code on a task.
2. Charon creates a room or devop workstream for the task.
3. Claude Code User agent translates the task into a good Claude Code instruction.
4. Charon sends that instruction to the Claude Code session.
5. Charon watches output and detects idle/completion/waiting/stall.
6. Judge evaluates result against repo state/evidence.
7. If weak, operator asks Claude Code for another pass.
8. If good, Charon records checkpoint/review and continues.

## Why this reuses current architecture
- Rooms/groups already model turn-taking and participant identity.
- External sessions/boats already exist for Hermes/pi/Charon.
- Devop runtime already supports checkpoints/reviews/evidence.
- Dashboard and sessions UI already expose external session state.

The missing piece is a robust **Claude Code transport/control adapter** plus room semantics for an external coding participant.

## MVP scope

### In scope
- attach/launch Claude Code session via tmux/boat-style wrapper
- send textual instructions to Claude Code
- capture output stream and summarize it
- infer basic state: running / idle / complete / stalled / waiting_for_input
- create a room kind for Claude Code user flows
- operator agent + judge room pairing
- persist room events and compact review summaries

### Out of scope for MVP
- full devop runtime integration for every room path
- deep F4 graph model changes beyond room visibility
- perfect completion detection
- multi-session load balancing
- custom structured Claude Code protocol (if not natively available)

## Critical engineering problem
The hard part is not rooms. It is making Claude Code a reliable external participant.

We need to answer:
- how to send work
- how to read output
- how to detect completion / waiting / stall
- how to interrupt or steer
- how to recover if output parsing fails

## Recommended implementation phases

### Phase A — Claude Code adapter foundation
Create `apps/core-daemon/claude_code_adapter.py`.

Responsibilities:
- launch or attach to a Claude Code session
- send text input to the session
- subscribe to/capture output
- normalize status snapshots
- expose helper methods:
  - `launch_claude_code_session(...)`
  - `attach_claude_code_session(...)`
  - `send_instruction(...)`
  - `capture_until_idle(...)`
  - `detect_session_state(...)`
  - `interrupt(...)`

Implementation notes:
- start with tmux/boat-backed integration, matching existing wrapped session patterns
- use output heuristics similar to existing boat capture code in `chat_backend.py`
- persist adapter-visible state under `.charon_state/claude_code/...`

### Phase B — Claude Code room participant
Create `apps/core-daemon/claude_code_room_participant.py`.

Responsibilities:
- represent Claude Code as a room participant object
- store participant metadata:
  - provider=`claude_code`
  - session_name / tmux_session / socket / transport
  - state=`idle|running|waiting|stalled|complete|error`
- append normalized participant events into room event stream
- support steering/injection into the Claude Code session

### Phase C — Claude Code user room runner
Create `apps/core-daemon/claude_code_user.py`.

Responsibilities:
- Charon operator prompt/behavior for Claude Code use
- transform high-level tasks into Claude Code instructions
- decide when to retry / repair / summarize
- emit room events like:
  - `claude_code_instruction_sent`
  - `claude_code_output_captured`
  - `claude_code_waiting_detected`
  - `claude_code_stall_detected`
  - `claude_code_summary_recorded`

### Phase D — Judge integration
Use existing judge concepts to review the Claude Code pass.

MVP path:
- room-local judged summary first
- then connect to `devop_runtime.py` for checkpoint/review persistence when launched as a software-dev operation

Room review outputs should include:
- accept / repair_requested / blocked
- concise critique
- evidence summary
- changed files / test/build hints when available

### Phase E — command/API surface
Add commands such as:
- `/claude-code attach <session>`
- `/claude-code launch [name]`
- `/claude-code use <goal>`
- `/claude-code room <goal>`
- `/claude-code status <room-or-session>`

Natural-language routing later:
- "use Claude Code on this feature"
- "have Claude Code implement this but supervise it"
- "start a coding room with Claude Code as implementer"

## Concrete module targets

### New modules
- `apps/core-daemon/claude_code_adapter.py`
- `apps/core-daemon/claude_code_room_participant.py`
- `apps/core-daemon/claude_code_user.py`

### Existing modules to integrate
- `apps/core-daemon/inter_agent_rooms.py`
- `apps/core-daemon/external_session_launcher.py`
- `apps/tui/opentui/chat_backend.py`
- `apps/core-daemon/devop_runtime.py` (phase D)
- `apps/core-daemon/judge_engine.py` (phase D)

## Transport options

### Preferred MVP path
Use a wrapped tmux/boat Claude Code session.

Why:
- existing project already has wrapped external session patterns
- session discovery/UI support already exists
- control path can reuse existing input/output capture logic

### Required adaptation
`external_session_launcher.py` currently does not support Claude Code. Add support later if Claude Code executable/entry path is stable.

## Completion / stall detection heuristics
Initial heuristics are acceptable for MVP.

State signals to detect:
- recent output still arriving -> `running`
- meaningful output quiet period after work -> `complete_or_idle`
- explicit waiting text / prompt footer -> `waiting_for_input`
- long quiet period without known completion -> `stalled`

Persist raw capture snippets plus normalized state transitions.

## Room/event schema additions
Add event types such as:
- `claude_code_session_attached`
- `claude_code_instruction_sent`
- `claude_code_output_chunk`
- `claude_code_output_summary`
- `claude_code_state_changed`
- `claude_code_judge_review`
- `claude_code_repair_requested`
- `claude_code_accepted`

## Manual test scenarios
1. Launch/attach Claude Code session.
2. Send simple repo question.
3. Detect completion and summarize output.
4. Run room with operator + Claude Code + judge.
5. Force judge repair and resend refined instruction.
6. Verify room events persist and show in F4 room list.

## Success criteria for MVP
- Charon can launch or attach Claude Code.
- Charon can send instructions and capture output.
- A room can include Claude Code as a participant.
- A judge can critique Claude Code output.
- A second pass can be requested automatically.
- Basic state is visible in room/session/dashboard surfaces.
