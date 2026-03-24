# Onboarding Compatibility Contract (V1)

Status: draft
Schema version: 1.0

Goal
- Keep onboarding and /setup stable while adding agent-network capabilities.

Existing fields (must remain)
- complete
- step
- provider_mode
- provider
- model
- provider_auth
- project
- updated_at

New additive fields
- default_agent_name (string, default: charon-main)
- enable_remote_links (bool, default: false)
- default_link_scope (local|remote|hybrid, default: local)
- diagnostics_mode (off|on, default: off)

Compatibility rules
1) Missing new fields auto-default on load.
2) Existing onboarding.json files load without migration failure.
3) /setup status shows both old and new fields.

Setup commands that must remain valid
- /setup provider <name>
- /setup no-provider
- /setup model <name>
- /setup project <name>
- /setup complete
- /setup status
- /setup reset

Behavior guardrails
- provider/no-provider gates model execution, not agent management UI.
- onboarding completion must not require remote linking.
- diagnostics defaults off so Shades remain internal in normal UX.
