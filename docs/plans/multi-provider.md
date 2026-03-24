# Multi-Provider Design

> Support multiple LLM providers simultaneously. Authenticate once per provider,
> assign providers to agents, switch mid-session.

## User Experience

### Launch shortcuts
```bash
charon                  # Uses default provider
charon claude-code      # New agent with Claude
charon codex            # New agent with Codex
charon lmstudio         # New agent with local LM Studio
charon opencode         # New agent with OpenCode config
```

If the provider isn't authenticated yet, Charon opens directly to that
provider's onboarding (OAuth link or config step).

### In-session switching
```
/provider codex         # Switch current agent to Codex
/model o3               # Switch model (within current provider)
/model claude-opus-4.6  # Switch to different provider's model (auto-detects provider)
```

### Status bar
```
  charon  ↑1240 ↓580  ctx:12%    F1:chat  F2:dashboard  F3:sessions    ○ claude-code/claude-sonnet-4.6  effort:medium
```

## Data Model

### onboarding.json (updated)
```json
{
  "providers": {
    "claude-code": {
      "authenticated": true,
      "default_model": "claude-sonnet-4.6",
      "last_used": "2026-03-20T00:00:00Z"
    },
    "codex": {
      "authenticated": true,
      "default_model": "o3",
      "last_used": "2026-03-19T00:00:00Z"
    },
    "lmstudio": {
      "authenticated": true,
      "default_model": "qwen3-30b-a3b",
      "last_used": "2026-03-18T00:00:00Z"
    }
  },
  "default_provider": "claude-code",
  "project": "/home/dopppo/Projects/charon",
  "complete": true
}
```

### auth.json (unchanged — already supports multiple)
```json
{
  "version": 1,
  "active_provider": "anthropic",
  "providers": {
    "anthropic": { "tokens": {...}, "auth_type": "oauth" },
    "openai-codex": { "tokens": {...}, "auth_type": "oauth" }
  }
}
```

### Agent schema (add provider/model fields)
```json
{
  "id": "AG-0001",
  "name": "charon-main-01",
  "provider": "claude-code",
  "model": "claude-sonnet-4.6",
  "project": "/home/dopppo/Projects/charon",
  "goal": "Primary agent",
  ...
}
```

## Implementation

### Phase 1: Data model (agent workstream)
- [ ] Update onboarding.json schema to `providers` dict
- [ ] Migration: convert old single-provider format to new format
- [ ] Add `provider` and `model` fields to agent schema
- [ ] Update provider_bridge.py to accept provider name parameter
- [ ] Update agent_runtime.py to read provider from agent, not global config

### Phase 2: Entry point (UI workstream)
- [ ] Update `charon` script to accept provider argument
- [ ] Route `charon claude-code` to create agent with that provider
- [ ] If provider not authenticated, jump to that provider's onboarding
- [ ] Update status bar to show per-agent provider/model

### Phase 3: In-session switching (both workstreams)
- [ ] `/provider <name>` command — switches current agent's provider
- [ ] `/model <name>` — if model belongs to different provider, auto-switch
- [ ] Recreate ConversationEngine when provider changes
- [ ] Persist provider/model changes to agent record

### Phase 4: Dashboard integration (UI workstream)  
- [ ] Show provider badge on each agent in dashboard
- [ ] Color-code by provider (Claude=purple, Codex=green, local=blue)
- [ ] Provider filter in agent list

## Backward Compatibility

The old single-provider `onboarding.json` format is auto-migrated:
```python
# Old format
{"complete": true, "provider": "claude-code", "model": "claude-sonnet-4.6", ...}

# Migrated to
{"complete": true, "default_provider": "claude-code", 
 "providers": {"claude-code": {"authenticated": true, "default_model": "claude-sonnet-4.6"}}, ...}
```

Old agents without `provider`/`model` fields inherit from `default_provider`.
