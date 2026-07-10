"""Consolidation and agent-ledger mixin."""
from __future__ import annotations

from backend import common


class ConsolidationMixin:
    """Memory-consolidation and agent-ledger request handlers."""

    def handle_consolidation_traces(self, request_id: str | None):
        """Return recent consolidation scan traces for dashboard display."""
        try:
            from charon.memory.consolidation import list_traces
            traces = list_traces(common.STATE_DIR, limit=20)
            common.emit({
                'type': 'consolidation_traces',
                'traces': traces,
                'request_id': request_id,
            })
        except Exception as e:
            common.emit({'type': 'error', 'error': f'Failed to load traces: {e}', 'request_id': request_id})

    def handle_consolidation_config(self, msg: dict, request_id: str | None):
        """Get or update consolidation config."""
        from charon.memory.consolidation import load_config, save_config
        action = msg.get('action', 'get')
        if action == 'get':
            config = load_config(common.STATE_DIR)
            common.emit({
                'type': 'consolidation_config',
                'config': config,
                'request_id': request_id,
            })
        elif action == 'set':
            updates = msg.get('config', {})
            config = load_config(common.STATE_DIR)
            config.update(updates)
            save_config(common.STATE_DIR, config)
            common.emit({
                'type': 'consolidation_config',
                'config': config,
                'message': 'Config updated.',
                'request_id': request_id,
            })

    def handle_agent_ledger(self, agent_id: str, request_id: str | None):
        """Return task history for an agent."""
        if not agent_id:
            # Default to the primary charon agent
            try:
                from charon.agents.agent_lifecycle import list_agents
                for a in list_agents():
                    if a.get('role') == 'charon' and a.get('status') != 'stopped':
                        agent_id = a.get('id', '')
                        break
            except Exception:
                pass
        try:
            from charon.agents.task_ledger import get_agent_ledger_summary
            result = get_agent_ledger_summary(common.STATE_DIR, agent_id)
            common.emit({
                'type': 'agent_ledger',
                'agent_id': agent_id,
                'entries': result['entries'],
                'stats': result['stats'],
                'request_id': request_id,
            })
        except Exception as e:
            common.emit({'type': 'error', 'error': f'Ledger failed: {e}', 'request_id': request_id})

    def handle_consolidation_run(self, request_id: str | None):
        """Manually trigger a consolidation scan."""
        try:
            from charon.memory.consolidation import load_config, run_consolidation
            config = load_config(common.STATE_DIR)
            result = run_consolidation(common.STATE_DIR, config)
            changes = result.get('changes', [])
            common.emit({
                'type': 'consolidation_result',
                'trace': result,
                'message': f'Consolidation complete: {len(changes)} changes, {result.get("events_processed", 0)} events processed.',
                'request_id': request_id,
            })
        except Exception as e:
            common.emit({'type': 'error', 'error': f'Consolidation failed: {e}', 'request_id': request_id})
