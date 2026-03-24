# Voice + Onboarding + Agent Loop Priority Plan

> For Hermes: implement in priority order. Do not let voice feature work block core agent usability.

Goal
- Ship an initial useful working state quickly: stable chat agent loop + onboarding screen first.
- Keep voice as a foundational architecture concern, but phase implementation to avoid delaying first release.

Architecture Direction
- Treat output as typed messages (status_update, conversational, alert/error) so voice routing is explicit.
- Keep local-first assumptions for TTS/STT.
- Build onboarding as the front door (first-run setup and toggles) on top of the Charon title/mascot page.

Current Priority (MVP)
1) Core agent behavior loop (Hermes-like workflow)
2) Onboarding UX in title page
3) Push initial useful release
4) Voice/STT Phase 1 immediately after MVP merge

---

## Phase 0 — Must Ship First (Now)

### Task 0.1: Stabilize chat agent loop behavior
Objective: Get reliable request/response behavior close to Hermes agent flow.

Scope
- Ensure chat submission path is robust in Textual UI.
- Preserve short status messages during coding tasks.
- Keep command handling reliable (/agent, /model, mode switches).

Verification
- Manual: run `charon`, send normal and command messages, verify no dead states.
- Automated: existing tests pass.

### Task 0.2: Implement onboarding screen behavior
Objective: Title page becomes setup + onboarding control surface.

Scope
- Add onboarding panel layered over mascot/title area.
- Keep input visible/usable at bottom at all terminal heights.
- Add setup prompts for initial model and basic preferences.

Verification
- Small terminal: mascot tiny/hidden but input still visible.
- Medium/large terminal: onboarding panel renders consistently.

### Task 0.3: MVP release checkpoint
Objective: Freeze a useful first public state.

Definition of Done
- Agent loop feels usable end-to-end.
- Onboarding no longer feels placeholder.
- No blocking layout regressions in narrow terminals.

---

## Phase 1 — Voice Foundation (Immediately after MVP)

### Task 1.1: Persist voice/stt settings
Scope
- Add `.charon_state/settings.json` fields:
  - voice_enabled
  - voice_mode (off|concise|full)
  - tts_backend (local)
  - tts_voice_id/path
  - stt_enabled
  - stt_mode (off|push_to_talk|always_listen_experimental)

### Task 1.2: Message classification + TTS routing
Scope
- Introduce message-type policy:
  - status_update -> concise speech in coding mode
  - conversational -> full speech in full mode
  - alert/error -> short high-priority speech
- Route UI text and TTS text separately.

### Task 1.3: Local TTS integration (baseline)
Scope
- Wire local Piper execution wrapper.
- Add `/voice on|off`, `/voice mode concise|full`, `/voice test`.

### Task 1.4: Local STT integration (baseline)
Scope
- Add initial local STT wrapper with push-to-talk first.
- Add `/stt on|off`, `/stt mode push`.

---

## Phase 2 — Advanced Audio Features (Planned, not MVP-blocking)

- Wake word activation (local keyword spotter)
- Interruption/barge-in detection during TTS
- Earcons/sound effects for task lifecycle/listening/error
- Improved conversational prosody and pacing policies
- Custom Charon voice profile workflow (local voice tuning)
- Always-listen STT mode hardened for daily use

---

## Product Behavior Policy (Agreed)

1) During coding/implementation work:
- Agent emits short, separate updates by default.
- Speech is concise and notification-style.

2) During conversational/help mode:
- Agent can produce fuller responses.
- In voice full mode, full response is spoken.

3) Agent decides speech granularity per message type.

---

## Release Guardrails

- Do not block MVP on advanced audio features.
- Keep local-first as default assumption for both TTS and STT.
- Ensure narrow terminal behavior never hides the input box.
- Maintain parity with Hermes-like practical workflow over UI polish-only work.
