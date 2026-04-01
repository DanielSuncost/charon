# Conversation room controls and steering

Date: 2026-03-29

## Scope

Improve Hermes conversation rooms with three related capabilities:

1. Pause/resume room runners
2. User interjection / steering into active rooms
3. Softer participant prompting with turn policy separated from content style

## Backend design

Primary files:

- `apps/tui/opentui/chat_backend.py`
- `apps/core-daemon/inter_agent_rooms.py`

## Implemented shape

### Room pause/resume

Added commands:

- `/pause-room <room-id>`
- `/resume-room <room-id>`
- `/say-room <room-id> <message>`

Behavior:

- Room status is persisted in `room.json`
- Runner loops now treat `status=paused` as a wait state instead of a terminal state
- Runner state is persisted under `room.meta.runner_state`
- On resume, Charon restarts the runner thread if needed and continues from saved turn state

Persisted runner state currently includes:

- `mode`
- `turn`
- `silent_turns`
- `last_utterance`
- `started`
- `current_role` or `current_idx`

### F4 room controls

Added an initial F4 room view in the OpenTUI.

Current controls:

- `F4` to open room controls
- `↑↓` select room
- `p` pause/resume selected room, with immediate local UI feedback
- `i` jump back to chat with a next-turn `/inject-room ...` command prefilled
- `Enter` jump back to chat with `/say-room ...` prefilled
- `d` delete the selected room and close its participant sessions
- `r` refresh

### User interjection / steering

Added command:

- `/inject-room <room-id> [--target whole|teacher|student|<participant>] [--when now|next] <message>`

Behavior:

- `/say-room` is the clearest user-facing path for speaking into the shared conversation
- Interjections are stored in `room.meta.pending_injections`
- Whole-room messages now stay pending until they have been delivered to each participant once
- They can target:
  - whole room
  - teacher
  - student
  - a participant id / name / session substring
- `--when now` is currently best-effort: it is prioritized for the next runnable turn, not a hard mid-generation abort
- Matching interjections are consumed when the targeted participant is about to speak
- Applied interjections are appended into the turn prompt as room steering notes

### Better prompts

Changes:

- Participant seed prompts are softer and less prescriptive
- Turn-taking remains owned by room orchestration logic
- Content/tone guidance is lighter
- Forced “end with one question” behavior was removed from the default room prompts
- Room injections can steer direction without rewriting persona text

## Follow-ups

Nice next steps:

- Expand the new F4 room controls beyond the initial list/detail + pause/resume + inject affordances
- Surface queued injections and paused state more prominently in the room UI
- Support true immediate interruption if the underlying wrapped session can be safely interrupted mid-turn
- Add manual room messages that appear as explicit room events distinct from steering
