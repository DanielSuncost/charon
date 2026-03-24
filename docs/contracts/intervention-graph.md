# Intervention Graph Contract (V1)

Status: draft
Schema version: 1.0 (event.schema extensions)

Goal
- Track exact points where one agent intervenes in another agent's conversation flow.
- Enable deterministic backtracking and branch replay.

Core fields (event-level)
- conversation_id: Stable thread identifier.
- message_id: Unique message node id.
- parent_message_id: Direct parent in the thread DAG.
- intervention_of_message_id: Target node being intervened on (null for normal message).
- causation_id: Causal source id (usually parent or intervention target).
- branch_label: Optional operator label (e.g. "hotfix", "counterproposal").

Event types
- agent_message: Standard agent emission in a thread.
- agent_intervention: Cross-agent intervention tied to a specific target message.
- agent_backtrack: Operator or agent requested replay/path reconstruction.

Example: intervention event
```json
{
  "schema_version": "1.0",
  "id": "evt-9834",
  "ts": "2026-03-16T00:30:00Z",
  "event_type": "agent_intervention",
  "actor_type": "agent",
  "actor_id": "AG-0002",
  "correlation_id": "conv-42",
  "causation_id": "msg-a1",
  "conversation_id": "conv-42",
  "message_id": "msg-b7",
  "parent_message_id": "msg-a1",
  "intervention_of_message_id": "msg-a1",
  "branch_label": "schema-guard",
  "payload": {"content": "Pause: parser change must preserve contract v1."},
  "signature": null
}
```

Backtracking semantics
1. Start from target `message_id`.
2. Follow `parent_message_id` repeatedly to root.
3. Return ordered path root -> target.
4. If a parent is missing, return partial path with integrity warning.
