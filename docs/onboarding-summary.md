# Charon Onboarding – Design + Status Summary

## Goal
Provide a fast, guided setup to either:
1) Run Charon agents with a chosen provider (Codex / Claude Code / OpenRouter API / OpenCode)
2) Run Charon as a coordination layer only (no provider), by detecting already-running agents

## Intended Flow (UI‑agnostic)
1) /setup (or initial landing action)
   - Two choices:
     - provider = use Charon agents
     - no‑provider = manage existing agents
2) If provider:
   - Select provider: codex, claude-code, opencode, api
   - codex/claude-code → OAuth login (cleanroom)
     - Show auth URL
     - User opens link in browser
     - Local callback captures tokens automatically
     - Tokens stored in .charon_state/auth/auth.json
   - api → prompt for API key (OpenRouter), optional base URL
   - opencode → read ~/.config/opencode/opencode.json, choose provider + model
3) Set model (for API‑based providers)
4) Set project name
5) Complete onboarding:
   - create default “charon-main” agent
   - detect other agents on the machine
   - switch to dashboard with agents list

## Provider Storage
- Auth + provider selection stored in .charon_state/auth/auth.json
- Onboarding state stored in .charon_state/onboarding.json
- Opencode config read from ~/.config/opencode/opencode.json
- API key stored in auth.json (OpenRouter)

## OAuth (Cleanroom)
- Implemented in src/charon/providers/charon_auth.py
- PKCE + local callback server
- Codex: OpenAI OAuth endpoints
- Claude Code: Anthropic OAuth endpoints
- No manual code entry required (browser redirect hits localhost)

## Agent Detection
- Process inspector implemented (detects running “hermes/opencode/pi/openclaw/claude/codex” via ps)
- Intended to run for “no‑provider” path and on setup completion

## Current Status
- Cleanroom OAuth module implemented (charon_auth.py)
- Onboarding state machine paths for provider/no‑provider/api-key/opencode implemented
- Auth token store secured (chmod 700 dir, 600 file)
- Agent detection exists and can be hooked on completion
- Textual UI got unstable; moving to bun/opentui for the interface

## Remaining Work (UI‑agnostic)
1) Implement onboarding UI in bun/opentui:
   - command picker for /setup
   - provider selection menu
   - auth URL display with Open/Copy
2) Wire the logic to the new UI:
   - call charon_auth.login_oauth for codex/claude
   - parse opencode.json for opencode
   - store OpenRouter key in auth.json
3) Completion step:
   - create default charon agent
   - detect running agents
   - switch to dashboard
