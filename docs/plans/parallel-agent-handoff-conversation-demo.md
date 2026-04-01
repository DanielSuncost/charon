# Parallel Agent Handoff — Hermes Conversation Demo

Date: 2026-03-29
Owner of core integration: primary Charon Rust/TUI agent

## Goal

Deliver a demo where the user can say:

> start a conversation between two hermes agents. one is the teacher explaining a recent self-distillation paper to the other, a student who wants to understand it. the conversation continues until I stop it.

And then:

1. A room is created automatically.
2. Two Hermes participants are created with roles.
3. `F4` immediately shows the room.
4. The room shows **two live participant panes**.
5. The conversation actually plays out between the two Hermes agents.
6. Turn taking is visible.
7. The currently speaking pane is highlighted.
8. The conversation continues until the user stops it.

## Very Important Constraint

The **core live conversation path** is fragile and should remain owned by one agent.

Other agents should **not** modify the parts of the system that:
- spawn and bind Hermes room participants
- drive turn-taking between participants
- send input into boat sessions
- wait for output from boat sessions
- decide whether a turn succeeded or timed out
- define room-runner control flow for start/continue/stop

Those areas are the most likely to break demo readiness if multiple agents touch them at once.

## Core Path Reserved To Primary Agent

The following files/areas are effectively reserved:

### Reserved backend areas
- `apps/tui/opentui/chat_backend.py`
  - `_create_hermes_room(...)`
  - `_start_conversation_room_runner(...)`
  - boat input/output helpers for room runners
  - room runner stop/continue semantics
  - final NL orchestration short-circuit if it directly affects runner startup timing

### Reserved frontend integration areas
- `crates/charon-tui/src/main.rs`
  - F4 room pane attachment logic if it directly impacts live room panes
  - polling logic for live room panes
  - any code that could destabilize F3/F4 pane attachment or session identity

## Safe Delegation Areas

The following tasks are safe to hand off because they can be developed with low risk of blocking the core conversation loop.

---

# Task A — Natural-Language Routing Hardening

## Purpose
Make orchestration requests reliably route into the explicit command path instead of falling through into normal chat reasoning.

## Scope
Allowed files:
- `apps/tui/opentui/chat_backend.py`

Allowed areas:
- prompt classification / orchestration-intent detection
- visible status messages like:
  - `Routing orchestration request to /conversation ...`
- parsing demo-like phrasing into canonical commands

## Do
- Strengthen detection for prompts like:
  - “start a conversation between two hermes agents...”
  - “one is the teacher... the other is the student...”
  - “the conversation continues until I stop it”
  - “teacher explains a paper to student”
- Route those prompts to:
  - `/conversation hermes teacher student <topic>`
- Emit a status event confirming the route happened.
- Prefer orchestration whenever the user asks to create/spawn/start agents, rooms, teams, or sessions.

## Do Not
- Do not modify `_create_hermes_room(...)`
- Do not modify `_start_conversation_room_runner(...)`
- Do not modify boat socket input/output waiting logic
- Do not add subprocess/Bash-based fallback hacks
- Do not change room-runner timing or progression

## Definition of Done
- The demo prompt never falls into the normal conversation engine.
- The user sees an immediate routing status.
- The backend directly invokes the canonical command path.

---

# Task B — F4 Conversation Room UI Polish + Active Speaker Highlighting

## Purpose
Make F4 visually ready for the demo without changing the underlying conversation runner.

## Scope
Allowed files:
- `crates/charon-tui/src/app.rs`
- `crates/charon-tui/src/main.rs`
- `crates/charon-tui/src/render.rs`

Allowed areas:
- conversation-room presentation in F4
- role labels in pane titles
- current-speaker highlighting
- placeholder/empty/waiting states
- event log readability

## Do
- Improve the conversation-room F4 layout for exactly two participants.
- Ensure titles clearly show roles, e.g.:
  - `Hermes Teacher`
  - `Hermes Student`
- Highlight the currently speaking participant based on room events such as:
  - `conversation_turn_started`
  - `participant_output`
  - `turn_timeout`
- Make it clear when a participant is:
  - waiting
  - active
  - timed out
- Keep the event log visible and readable.

## Do Not
- Do not change how room panes attach to boat sessions if doing so risks live pane breakage.
- Do not change session identity matching rules.
- Do not change boat/Charon/Tmux backend transport code.
- Do not modify runner start/stop logic.
- Do not change F3 behavior.

## Definition of Done
- A 2-participant conversation room in F4 is visually clear.
- The active speaker pane is obvious.
- Waiting/timeout states are visible.
- Libris room rendering remains intact.

---

# Task C — Slash Command / Discoverability Polish

## Purpose
Make the forced orchestration commands easy to find and use.

## Scope
Allowed files:
- `crates/charon-tui/src/chat.rs`
- `apps/tui/opentui/chat_backend.py`

Allowed areas:
- slash suggestion catalog
- help command listings
- command descriptions

## Do
- Ensure the following are discoverable and correctly described:
  - `/conversation hermes teacher student <topic>`
  - `/conversation hermes 2 <topic>`
  - `/team hermes <count> <topic>`
  - `/devteam hermes <count> <goal>`
  - `/libris <prompt>`
- Improve suggestions for partial inputs such as:
  - `/conv`
  - `/conversation`
  - `/team`
  - `/devteam`
  - `/libris`
- Make command descriptions explicitly say that they create rooms / live participants.

## Do Not
- Do not alter the runner implementation.
- Do not alter boat transport code.
- Do not alter F4 pane polling logic.

## Definition of Done
- Users can discover the orchestration commands from the slash menu or `/help`.
- Suggestions guide the user toward the forced path instead of freeform prompting.

---

# Task D — Phase 2 TTS Prototype (Decoupled Only)

## Purpose
Start phase 2 without interfering with phase 1 demo readiness.

## Scope
Allowed files:
- new files/modules only, or isolated files under docs / experimental code paths
- optionally a new module under `apps/core-daemon/` if fully decoupled

## Do
- Prototype a local TTS subsystem that can accept:
  - `room_id`
  - `participant_id`
  - `text`
- Support different voices per participant.
- Assume local model/runtime.
- Keep it queue-based and decoupled.
- Prefer an adapter shape that could later consume room events like `participant_output`.

## Do Not
- Do not wire audio playback into the active room runner yet.
- Do not modify phase 1 conversation control flow.
- Do not add blocking audio generation to the backend loop.

## Definition of Done
- There is a standalone prototype with a clean API surface.
- No risk to the phase 1 live conversation demo.

---

## Current Known State / Context

### Current room/backend reality
- Conversation rooms now exist as a shared room type in F4.
- Hermes sessions can be spawned as boat-wrapped PTY sessions.
- Rooms can carry participant session IDs.
- F4 can render room-associated live panes.
- The runner has recently been upgraded from blind dispatch to output-aware turn handling.

### Current failure modes already observed
- Natural-language prompts sometimes still fall into the normal chat engine instead of orchestration.
- Rooms can show bookkeeping events without proving that Hermes actually replied.
- The demo only counts as successful if the participant panes show real Hermes output.

### Therefore
The highest priority remains:
1. real Hermes output appears in participant panes
2. F4 shows the live conversation
3. active speaker is highlighted
4. the conversation keeps going until stopped

Everything else is secondary.

## Non-Goals For Parallel Agents

Parallel agents should **not** spend time on:
- Bun/OpenTUI integration
- replacing the Python backend
- broad F1 styling work
- general F3 feature work
- generic room architecture rewrites
- deep Libris graph changes
- a full TTS integration into live room playback

Those can wait.

## Suggested Workflow

### If assigned Task A
- only touch NL orchestration parsing
- commit minimal, surgical changes
- avoid changing command execution internals

### If assigned Task B
- only touch F4 presentation state and visuals
- avoid touching session attach logic except display-level state

### If assigned Task C
- only touch help/suggestion plumbing
- do not change runner behavior

### If assigned Task D
- build standalone or behind a feature flag
- do not integrate into the live runner path yet

## Demo Success Criteria

The demo is successful when:
- saying the demo prompt triggers orchestration immediately
- a room appears in F4
- two Hermes panes appear immediately
- real conversation text appears in both panes
- speaker highlighting updates as turns alternate
- the room continues until the user stops it

## Stop Conditions / Escalation

If a delegated agent finds that their task requires changing any of the reserved core areas, they should stop and report that fact instead of pushing speculative edits.

Examples:
- needing to change `_start_conversation_room_runner(...)`
- needing to change boat output waiting semantics
- needing to alter F4 live pane attachment identity logic
- needing to rework transport behavior

Those changes should come back to the primary agent.

## Recommended Assignment Split

### Primary agent
- core Hermes conversation runner
- boat output detection
- room start/continue/stop control flow
- end-to-end demo integration

### Parallel agent 1
- Task A: NL orchestration routing hardening

### Parallel agent 2
- Task B: F4 active speaker highlighting + layout polish

### Parallel agent 3
- Task C or D:
  - slash command discoverability polish
  - or decoupled TTS prototype

## Handoff Summary

If you are a parallel agent, the safest and most helpful thing you can do is:
- improve **routing**, **visibility**, or **discoverability**
- without touching the fragile **conversation execution core**

That is how you help accelerate the demo without impeding it.
