# Command Contracts (V1)

Status: draft
Schema version: 1.0

Principles
- User-facing control surface is persistent-agent-first.
- Internal Shades are never first-class user commands.
- Commands must work in Textual UI and CLI parity mode.

## Commands

- /agent create <name> [--project <project>|--generalist]
- /agent list
- /agent assign <agent> <project>
- /agent task <agent> <instruction>
- /agent link add <node>
- /agent link list
- /agent link revoke <node>
- /agent inbox <agent>
- /agent thread <agent>
- /agent intervene <target-agent> <message-id> <instruction>
- /agent backtrack <conversation-id> <message-id>

Rules
- No /shade command in user-facing surface.
- Shade activity is translated into agent-level status unless diagnostics mode is enabled.
- Unknown command returns stable parse error + suggestion.
- Unauthorized remote action returns explicit scope error.
- Every cross-agent intervention must emit a graph-linked event with `conversation_id`, `message_id`, `parent_message_id`, and `intervention_of_message_id`.
- `/agent backtrack` resolves the parent chain from target message back to root and returns replayable nodes.

Audit/event requirements
- /agent create -> agent_created
- /agent assign -> agent_assigned
- /agent task -> task_created/task_started/task_succeeded|task_failed
- /agent link add|revoke -> remote_link_enrolled|remote_link_revoked

- /agent intervene -> agent_intervention
- /agent backtrack -> agent_backtrack
