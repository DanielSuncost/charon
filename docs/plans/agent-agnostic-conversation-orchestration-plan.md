# Agent-Agnostic Conversation + Orchestration Plan

Date: 2026-03-29
Status: active
Owner: Charon core conversation/orchestration work

## Goal

Make conversation rooms, Libris, and future software-development orchestration agent/model agnostic.

That means any participant runtime that can:
- receive turn input
- expose streamed output
- provide session identity / transport metadata
- signal readiness and turn completion

should be usable in:
- `/conversation ...`
- `/team ...`
- Libris rooms
- future software-dev rooms

Examples:
- Hermes
- Charon
- pi
- Claude-backed wrapped agents
- Codex-backed wrapped agents
- mixed-agent rooms later

## Problems To Solve

### 1. Provider lock-in
Current conversation orchestration is effectively Hermes-specific.

### 2. Timing / coordination is too timeout-forward
Primary progression should be:
1. send turn input
2. detect meaningful assistant output
3. detect turn completion
4. hand off to next participant

Timeouts should be fail-safes only.

### 3. Output parsing is too transport-noise-sensitive
We need to separate:
- runtime / shell / progress noise
- actual assistant reply text

without banning legitimate content like model names.

### 4. Conversation taxonomy and system-family routing are underspecified
We need to distinguish:
- `conversation`
- `libris`
- `devteam`

and then choose a family-specific role/archetype.

## Architectural Direction

## A. Shared room substrate
Keep shared:
- room ids
- participants
- participant sessions / transport metadata
- room events
- room lifecycle
- F4 rendering / panes / event log

## B. System-family specific orchestration layers

### conversation
Lightweight visible turn-taking among participants.

### libris
Structured research orchestration with coordinator / researchers / shades.

### devteam
Structured software-work orchestration with planning / implementation / review roles.

## C. Participant runtime adapter layer
Each participant runtime gets an adapter.

Required adapter contract:
- spawn or attach participant session
- wait until ready
- send turn input
- capture output stream
- detect turn completion
- expose provider / model / transport metadata

## Proposed participant data model

```json
{
  "id": "agent-1",
  "name": "Hermes 1",
  "role": "peer-1",
  "agent_type": "hermes",
  "transport": "boat-pty",
  "session": "boat-hc-...",
  "capabilities": {
    "structured_completion": false,
    "tool_use": false,
    "streaming": true
  },
  "mode_profile": "conversation-peer"
}
```

## Proposed top-level routing schema

```json
{
  "target_system": "conversation|libris|devteam",
  "confidence": 0.0,
  "needs_clarification": false,
  "conversation_spec": {},
  "libris_spec": {},
  "devteam_spec": {}
}
```

## Conversation spec v1

```json
{
  "provider": "hermes|charon|pi|claude|codex",
  "participant_count": 2,
  "archetype": "peer|teacher-student|debate|researcher-reviewer|pair-programmers",
  "topic": "...",
  "style": "collaborative|adversarial|socratic|practical"
}
```

## Timing / coordination redesign

## Primary progression
The runner should progress on completed output, not elapsed time.

### Turn lifecycle
1. send prompt to current participant
2. read output stream
3. identify meaningful assistant text region
4. once meaningful text starts, keep collecting until output stabilizes
5. mark turn complete after a short quiet period in meaningful output
6. hand the completed utterance to next participant

## Timeouts become fail-safes
Use three layers:
- response-start timeout
- quiet-period completion timeout
- hard kill timeout

The first two are coordination heuristics; the last is emergency only.

## Output parsing redesign
Avoid topic-word blacklists.

Instead separate by format/source:
- shell prompts
- progress bars / percentages
- runtime status lines
- boat / session control noise
- actual assistant reply

## Near-term heuristic approach
Until adapters provide structured completion:
- detect first meaningful output chunk
- continue accumulating while meaningful output changes
- declare turn complete after a short quiet period
- preserve semantic content like model names if spoken as part of a real reply

## Long-term adapter improvement
Ideal adapter events:
- `turn_started`
- `text_delta`
- `turn_completed`

If an adapter provides these, the runner should use them directly.

## Role taxonomy

## Conversation archetypes v1
- `peer`
- `teacher-student`
- `debate`
- `researcher-reviewer`
- `pair-programmers`

## Libris role family
- `coordinator`
- `researcher`
- `shade`
- later `reviewer` / `judge`

## Devteam role family
- `planner`
- `implementer`
- `reviewer`
- `tester`
- `coordinator`

Important: overlapping names are allowed across families.
Examples:
- `conversation/researcher`
- `libris/researcher`

## Immediate implementation sequence

### Phase 1 — extract participant runtime abstraction
- Introduce participant runtime spec + adapter registry.
- Migrate Hermes path behind adapter interface without changing user-visible behavior.
- Keep current room runner semantics while replacing direct Hermes assumptions.

### Phase 2 — make turn completion output-driven
- Replace timeout-first progression with:
  - response-start detection
  - meaningful-output accumulation
  - quiet-period completion
- Keep hard timeouts as safeguards.

### Phase 3 — add non-Hermes providers
- Charon participant adapter
- pi adapter
- wrapped provider adapters for Claude / Codex when available

### Phase 4 — family-aware routing
- top-level classifier/rules choose `conversation` vs `libris` vs `devteam`
- conversation archetype selection chooses role preset
- ambiguous requests trigger clarification instead of wrong defaults

### Phase 5 — mixed-agent rooms
- allow heterogeneous participant lists
- e.g. Charon moderator + Hermes debaters

## Started now

The first implementation step being started is:
- define a provider-agnostic participant runtime abstraction module
- use it as the migration target for the existing Hermes-specific room code

This keeps current features working while we refactor toward:
- provider-agnostic conversations
- cleaner timing coordination
- eventual Libris/devteam reuse
