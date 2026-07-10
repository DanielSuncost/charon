"""Slash-command router mixin."""
from __future__ import annotations

import json
import re
import shlex
import time
from pathlib import Path

from backend import common
from backend.boat import _terminate_boat_session
from backend.dashboard import _load_workflow_steps_spec
from backend.nlparse import _parse_interval_phrase
from backend.settings_io import _full_messages_from_store, _load_project_registry, _load_ui_settings, _project_slug, _save_project_registry, _save_ui_settings
from backend.textutils import _sanitize_saved_messages

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


class CommandsMixin:
    """Slash-command catalog, suggestions, and the /command router."""

    def _command_catalog(self) -> list[dict]:
        """Return available commands with descriptions."""
        return [
            {'cmd': '/help', 'desc': 'Show available commands'},
            {'cmd': '/setup', 'desc': 'Show setup commands'},
            {'cmd': '/setup status', 'desc': 'Show current configuration'},
            {'cmd': '/setup reset', 'desc': 'Reset all configuration'},
            {'cmd': '/setup provider lmstudio', 'desc': 'Use local LM Studio'},
            {'cmd': '/setup provider claude-code', 'desc': 'Use Anthropic Claude (OAuth)'},
            {'cmd': '/setup provider codex', 'desc': 'Use OpenAI Codex (OAuth)'},
            {'cmd': '/setup provider api', 'desc': 'Use custom API endpoint'},
            {'cmd': '/setup model <name>', 'desc': 'Set model name'},
            {'cmd': '/setup api-key <key>', 'desc': 'Set API key directly'},
            {'cmd': '/setup project <path>', 'desc': 'Set project directory'},
            {'cmd': '/setup complete', 'desc': 'Finish setup'},
            {'cmd': '/setup no-provider', 'desc': 'Skip LLM setup (heuristic only)'},
            {'cmd': '/model', 'desc': 'Show current model'},
            {'cmd': '/reset', 'desc': 'Clear conversation'},
            {'cmd': '/dashboard', 'desc': 'Switch to dashboard (F2)'},
            {'cmd': '/sessions', 'desc': 'Switch to sessions (F3)'},
            {'cmd': '/chat', 'desc': 'Switch to chat (F1)'},
            {'cmd': '/hermes', 'desc': 'Launch a wrapped Hermes session in the background'},
            {'cmd': '/pi', 'desc': 'Launch a wrapped pi session in the background'},
            {'cmd': '/conversation hermes teacher student <topic>', 'desc': 'Start a teacher/student Hermes conversation room'},
            {'cmd': '/conversation hermes strategist critic <topic>', 'desc': 'Start a strategist/critic Hermes conversation room'},
            {'cmd': '/conversation hermes planner critic <topic>', 'desc': 'Start a planner/critic Hermes conversation room'},
            {'cmd': '/conversation hermes architect reviewer <topic>', 'desc': 'Start an architect/reviewer Hermes conversation room'},
            {'cmd': '/conversation hermes optimist skeptic <topic>', 'desc': 'Start an optimist/skeptic Hermes conversation room'},
            {'cmd': '/conversation hermes dialogue <topic>', 'desc': 'Start a peer philosophy dialogue between two Hermes agents'},
            {'cmd': '/conversation hermes 2 <topic>', 'desc': 'Start a 2-agent Hermes conversation room'},
            {'cmd': '/team hermes <count> <topic>', 'desc': 'Create a Hermes discussion room/team'},
            {'cmd': '/devteam hermes <count> <goal>', 'desc': 'Create a Hermes developer team room'},
            {'cmd': '/pause-room <room-id>', 'desc': 'Pause a conversation room runner'},
            {'cmd': '/resume-room <room-id>', 'desc': 'Resume a paused conversation room runner'},
            {'cmd': '/say-room <room-id> <message>', 'desc': 'Say something to the whole room so both sides can react'},
            {'cmd': '/inject-room <room-id> [--target whole|teacher|student|<participant>|coordinator|topic:<slug>|node:<agent-id>|researcher:<slug>|judge:<slug>|shade:<agent-id>] [--when now|next] <message>', 'desc': 'Inject steering or a message into a room'},
            {'cmd': '/libris <prompt>', 'desc': 'Start a Libris research intake for a broad research prompt'},
            {'cmd': '/libris status <operation_id>', 'desc': 'Inspect Libris swarm state for an operation'},
            {'cmd': '/devop <prompt>', 'desc': 'Start an autonomous software-development operation for a broad build prompt'},
            {'cmd': '/devop status <operation_id>', 'desc': 'Inspect software-dev operation status and workstreams'},
            {'cmd': '/devop stop <operation_id>', 'desc': 'Request stop for a software-dev operation'},
            {'cmd': '/monitor every hour <url>', 'desc': 'Create a recurring website health monitor'},
            {'cmd': '/automate every <n> <unit> check <url>', 'desc': 'Create a recurring automation for HTTP checking'},
            {'cmd': '/automate list', 'desc': 'List all automations'},
            {'cmd': '/automate list cron', 'desc': 'List cron-scheduled automations'},
            {'cmd': '/automate list continuous', 'desc': 'List always-on continuous automations'},
            {'cmd': '/automate list scheduled', 'desc': 'List interval/cron scheduled automations'},
            {'cmd': '/automate status <automation_id>', 'desc': 'Inspect an automation and recent runs'},
            {'cmd': '/automate pause <automation_id>', 'desc': 'Pause a recurring automation'},
            {'cmd': '/automate resume <automation_id>', 'desc': 'Resume a paused recurring automation'},
            {'cmd': '/automate stop <automation_id>', 'desc': 'Stop a recurring automation'},
            {'cmd': '/automate cron "0 9 * * 1-5" check <url>', 'desc': 'Create a cron-scheduled automation'},
            {'cmd': '/automate continuous every <n> seconds check <url>', 'desc': 'Create an always-on loop automation'},
            {'cmd': '/monitor browser every hour <url> expect "text"', 'desc': 'Create a browser-rendered functional monitor'},
            {'cmd': '/automate browser every <n> <unit> check <url> expect "text"', 'desc': 'Create a browser-based rendered-page monitor'},
            {'cmd': '/automate browser-workflow every <n> <unit> steps <json>', 'desc': 'Create a multi-step browser workflow automation'},
            {'cmd': '/automate browser-workflow every <n> <unit> from <file>', 'desc': 'Create a multi-step browser workflow automation from a JSON file'},
            {'cmd': '/automate webhook <automation_id> <url>', 'desc': 'Set a webhook for automation failure/recovery alerts'},
            {'cmd': '/harvest_souls', 'desc': 'Scan sibling agent repos for abilities to assimilate'},
            {'cmd': '/harvest_souls list', 'desc': 'Show numbered findings from last scan'},
            {'cmd': '/harvest_souls evaluate', 'desc': 'Evaluate real capability gaps from the last scan'},
            {'cmd': '/harvest_souls review', 'desc': 'Show capability-level harvest decisions'},
            {'cmd': '/harvest_souls decide', 'desc': 'Inspect one capability harvest decision'},
            {'cmd': '/harvest_souls harvest', 'desc': 'Queue capability clusters for assimilation'},
            {'cmd': '/harvest_souls harvest all', 'desc': 'Queue all recommended capability clusters'},
            {'cmd': '/harvest_souls plan', 'desc': 'Show implementation path for a raw ability'},
            {'cmd': '/harvest_souls adopt', 'desc': 'Legacy: mark raw abilities for adoption'},
            {'cmd': '/harvest_souls adopt all', 'desc': 'Legacy: adopt all raw discovered abilities'},
            {'cmd': '/harvest_souls roadmap', 'desc': 'Show adoption roadmap and progress'},
            {'cmd': '/harvest_souls status', 'desc': 'Show last scan summary'},
            {'cmd': '/harvest_souls hermes-agent', 'desc': 'Scan only hermes-agent'},
            {'cmd': '/voyage dispatch', 'desc': 'Dispatch a task to a remote agent worker'},
            {'cmd': '/voyage status', 'desc': 'Check status of a voyage'},
            {'cmd': '/voyage list', 'desc': 'List recent voyages'},
            {'cmd': '/fleet setup', 'desc': 'Set up a remote agent team (install, auth, start agents)'},
            {'cmd': '/fleet status', 'desc': 'Show fleet status'},
        ]

    def _get_suggestions(self, prefix: str) -> list[dict]:
        """Get matching commands for a prefix."""
        prefix = prefix.strip().lower()
        catalog = self._command_catalog()
        if not prefix or prefix == '/':
            return catalog

        starts = [c for c in catalog if c['cmd'].lower().startswith(prefix)]
        if starts:
            return starts[:30]

        token_matches: list[dict] = []
        needle = prefix.lstrip('/')
        for item in catalog:
            cmd = item['cmd'].lower()
            parts = [p for p in cmd.replace('/', ' ').replace('<', ' ').replace('>', ' ').split() if p]
            if any(part.startswith(needle) for part in parts):
                token_matches.append(item)
        if token_matches:
            return token_matches[:10]

        return []

    def handle_command(self, command: str, request_id: str | None):
        """Handle /setup and other slash commands."""
        command = command.strip()
        if not command:
            return

        try:
            if self._pending_provider_switch and command in ('/1', '/2'):
                pending = self._pending_provider_switch
                self._pending_provider_switch = None
                if command == '/1':
                    self._switch_provider_with_transfer(str(pending.get('target_provider') or ''), request_id)
                else:
                    self._switch_provider_fresh(str(pending.get('target_provider') or ''), request_id)
                return

            # /fleet setup interactive flow — handle pending responses
            if self._pending_fleet_setup:
                self._handle_fleet_setup_response(command, request_id)
                return

            # Show suggestions for /help, /setup alone, or unknown commands
            if command in ('/help', '/setup', '/?'):
                suggestions = self._get_suggestions('/setup' if command == '/setup' else '/')
                common.emit({
                    'type': 'suggestions',
                    'title': 'Setup Commands' if command == '/setup' else 'Available Commands',
                    'items': suggestions,
                    'request_id': request_id,
                })
                return

            if command == '/clarifications':
                try:
                    from clarify_tool import execute_clarify
                    from charon.tools import ToolContext
                    clar_ctx = ToolContext(project_root=common.ROOT, agent_id=(self._active_agent_id or ''), state_dir=common.STATE_DIR)
                    pending = execute_clarify({'action': 'list'}, clar_ctx)
                    details = pending.details or {}
                    items = details.get('items') or []
                    if not items:
                        common.emit({'type': 'status', 'message': 'No pending clarifications.', 'request_id': request_id})
                        return
                    suggestions = []
                    for row in items[:8]:
                        cid = str(row.get('clarification_id') or '')
                        question = str(row.get('question') or '')
                        choices = [str(c).strip() for c in (row.get('choices') or []) if str(c).strip()]
                        if 'Which provider should I use for worker tasks?' in question and choices:
                            for choice in choices:
                                suggestions.append({
                                    'cmd': f'/clarify {cid} {choice}',
                                    'desc': f'{question} → choose {choice}',
                                })
                        else:
                            suggestions.append({
                                'cmd': f'/clarify {cid} <answer>',
                                'desc': question,
                            })
                    common.emit({
                        'type': 'suggestions',
                        'title': 'Pending Clarifications',
                        'items': suggestions,
                        'request_id': request_id,
                    })
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Failed to load clarifications: {e}', 'request_id': request_id})
                return

            if command.startswith('/clarify '):
                rest = command[9:].strip()
                parts = rest.split(None, 1)
                if len(parts) != 2:
                    common.emit({'type': 'error', 'error': 'Usage: /clarify <clarification_id> <answer>', 'request_id': request_id})
                    return
                cid, answer = parts[0].strip(), parts[1].strip()
                try:
                    from clarify_tool import execute_clarify
                    from charon.tools import ToolContext
                    clar_ctx = ToolContext(project_root=common.ROOT, agent_id=(self._active_agent_id or ''), state_dir=common.STATE_DIR)
                    result = execute_clarify({'action': 'answer', 'clarification_id': cid, 'answer': answer}, clar_ctx)
                    if result.is_error:
                        common.emit({'type': 'error', 'error': result.content, 'request_id': request_id})
                    else:
                        applied = (result.details or {}).get('applied_result') or {}
                        msg = result.content
                        if applied:
                            msg += f' — applied worker provider {applied.get("provider")} ({applied.get("model")})'
                        common.emit({'type': 'status', 'message': msg, 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Clarification answer failed: {e}', 'request_id': request_id})
                return

            if command.startswith('/idea '):
                text = command[6:].strip()
                if not text:
                    common.emit({'type': 'error', 'error': 'Usage: /idea <description>', 'request_id': request_id})
                    return
                try:
                    from charon.memory import user_model_structured as ums
                    # Get message_seq from context store if available
                    msg_seq = -1
                    agent_id = self._active_agent_id or ''
                    if agent_id and self.engine:
                        msg_seq = len(self.engine.messages) - 1
                    idea = ums.record_idea(
                        common.STATE_DIR,
                        summary=text,
                        session_id=agent_id,
                        message_seq=msg_seq,
                        message_text=text,
                        source='explicit',
                    )
                    common.emit({'type': 'status', 'message': f'Recorded idea: {idea["summary"]} ({idea["id"]})', 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Failed to record idea: {e}', 'request_id': request_id})
                return

            if command == '/ideas':
                try:
                    from charon.memory import user_model_structured as ums
                    ideas = ums.list_ideas(common.STATE_DIR)
                    if not ideas:
                        common.emit({'type': 'status', 'message': 'No ideas recorded yet. Use /idea <text> to capture one.', 'request_id': request_id})
                    else:
                        lines = [f'Ideas ({len(ideas)} total):']
                        for i, idea in enumerate(ideas, 1):
                            tag = f' [{idea.get("category", "")}]' if idea.get('category', 'general') != 'general' else ''
                            src = '⚡' if idea.get('source') == 'auto' else '✏'
                            lines.append(f'  {src} #{i}{tag}: {idea.get("summary", "?")}  ({idea.get("id", "?")})')
                        common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Failed to list ideas: {e}', 'request_id': request_id})
                return

            if command.startswith('/idea-detail '):
                idea_id = command[13:].strip()
                if not idea_id:
                    common.emit({'type': 'error', 'error': 'Usage: /idea-detail <idea-id>', 'request_id': request_id})
                    return
                try:
                    from charon.memory import user_model_structured as ums
                    # Support both full id and #N shorthand
                    if idea_id.startswith('#'):
                        try:
                            idx = int(idea_id[1:]) - 1
                            all_ideas = ums.list_ideas(common.STATE_DIR)
                            if 0 <= idx < len(all_ideas):
                                idea_id = all_ideas[idx].get('id', '')
                            else:
                                common.emit({'type': 'error', 'error': f'Idea {idea_id} not found.', 'request_id': request_id})
                                return
                        except ValueError:
                            pass
                    result = ums.lookup_idea_context(common.STATE_DIR, idea_id)
                    if not result:
                        common.emit({'type': 'error', 'error': f'Idea {idea_id} not found.', 'request_id': request_id})
                        return
                    lines = [
                        f'Idea: {result.get("summary", "?")}',
                        f'ID: {result.get("id", "?")}  Category: {result.get("category", "?")}  Source: {result.get("source", "?")}',
                        f'Session: {result.get("session_id", "none")}  Message: #{result.get("message_seq", "?")}',
                        f'Captured: {result.get("created_at", "?")}',
                    ]
                    if result.get('message_text'):
                        lines.append(f'\nOriginal text:\n  {result["message_text"][:300]}')
                    ctx_msgs = result.get('context_messages', [])
                    if ctx_msgs:
                        lines.append('\nConversation context:')
                        for cm in ctx_msgs:
                            role = cm.get('role', '?')
                            content = cm.get('content', '')[:200]
                            lines.append(f'  [{role}] {content}')
                    common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Failed to look up idea: {e}', 'request_id': request_id})
                return

            if command.startswith('/setup '):
                rest = command[7:].strip()
                self._run_setup_command(rest, request_id)
                return
            if command == '/libris' or command.startswith('/libris '):
                rest = command[8:].strip() if command.startswith('/libris ') else ''
                if not rest:
                    self._pending_libris_intake = {
                        'prompt': '',
                        'goal_options': [],
                        'selected_goal': '',
                        'stop_condition': '',
                    }
                    common.emit({'type': 'status', 'message': 'Usage: /libris <broad research prompt>', 'request_id': request_id})
                    return
                if rest.startswith('status '):
                    op_id = rest[7:].strip()
                    try:
                        from charon.libris.libris_runtime import get_libris_swarm_state
                        swarm = get_libris_swarm_state(common.STATE_DIR, Path(self._libris_project_root()), op_id)
                        if not swarm:
                            common.emit({'type': 'error', 'error': f'No Libris operation found: {op_id}', 'request_id': request_id})
                            return
                        lines = [
                            f'Operation: {swarm.get("operation_id")}',
                            f'Status: {swarm.get("status")}',
                            f'Topics: {len(swarm.get("topics") or [])}',
                        ]
                        coord = swarm.get('coordinator') or {}
                        if coord:
                            lines.append(f'Coordinator: {coord.get("name")} [{coord.get("status")}]')
                        for topic in swarm.get('topics') or []:
                            lines.append(f'- {topic.get("title")} [{topic.get("status")}/{topic.get("phase")}]')
                        common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                    except Exception as e:
                        common.emit({'type': 'error', 'error': f'Libris status failed: {e}', 'request_id': request_id})
                    return
                if rest.startswith('use '):
                    choice = rest[4:].strip()
                    pending = self._pending_libris_intake or {}
                    options = list(pending.get('goal_options') or [])
                    if choice.isdigit() and 1 <= int(choice) <= len(options):
                        pending['selected_goal'] = options[int(choice) - 1]
                        self._pending_libris_intake = pending
                        common.emit({'type': 'status', 'message': f'Selected Libris goal: {pending["selected_goal"]}', 'request_id': request_id})
                    else:
                        common.emit({'type': 'error', 'error': f'Invalid Libris goal option: {choice}', 'request_id': request_id})
                    return
                if rest.startswith('custom '):
                    goal = rest[7:].strip()
                    if not goal:
                        common.emit({'type': 'error', 'error': 'Custom Libris goal cannot be empty.', 'request_id': request_id})
                        return
                    pending = self._pending_libris_intake or {}
                    pending['selected_goal'] = goal
                    self._pending_libris_intake = pending
                    common.emit({'type': 'status', 'message': f'Set custom Libris goal: {goal}', 'request_id': request_id})
                    return
                if rest.startswith('stop '):
                    cond = rest[5:].strip()
                    pending = self._pending_libris_intake or {}
                    pending['stop_condition'] = cond
                    self._pending_libris_intake = pending
                    common.emit({'type': 'status', 'message': f'Set Libris stop condition: {cond}', 'request_id': request_id})
                    return
                if rest == 'go':
                    self._start_libris_from_pending(request_id)
                    return

                pending = {
                    'prompt': rest,
                    'goal_options': self._libris_goal_options(rest),
                    'selected_goal': '',
                    'stop_condition': self._libris_extract_stop(rest),
                }
                self._pending_libris_intake = pending
                if self._libris_has_clear_goal(rest):
                    pending['selected_goal'] = rest
                self._emit_libris_intake(request_id)
                return
            if command == '/devop' or command.startswith('/devop '):
                rest = command[7:].strip() if command.startswith('/devop ') else ''
                if not rest:
                    common.emit({'type': 'status', 'message': 'Usage: /devop <broad software build prompt>', 'request_id': request_id})
                    return
                if rest.startswith('status '):
                    op_id = rest[7:].strip()
                    try:
                        from charon.devop.devop_projection import summarize_operation
                        from charon.devop.devop_runtime import get_operation_state
                        op = get_operation_state(common.STATE_DIR, op_id)
                        if not op:
                            common.emit({'type': 'error', 'error': f'No software-dev operation found: {op_id}', 'request_id': request_id})
                            return
                        summary = summarize_operation(common.STATE_DIR, op_id)
                        lines = [
                            f'Operation: {op.get("operation_id")}',
                            f'Status: {op.get("status")}',
                            f'Workstreams: {summary.get("workstream_count", 0)}',
                            f'Checkpoints: {summary.get("checkpoint_count", 0)}',
                            f'Reviews: {summary.get("review_count", 0)}',
                        ]
                        for ws in op.get('workstreams') or []:
                            latest_review = ws.get('latest_review') or {}
                            latest_checkpoint = ws.get('latest_checkpoint') or {}
                            lines.append(
                                f'- {ws.get("title") or ws.get("slug")} '
                                f'[{ws.get("status")}] '
                                f'cp={latest_checkpoint.get("checkpoint_id") or "-"} '
                                f'review={latest_review.get("decision") or "-"}'
                            )
                        common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                    except Exception as e:
                        common.emit({'type': 'error', 'error': f'Software-dev status failed: {e}', 'request_id': request_id})
                    return
                if rest.startswith('stop '):
                    op_id = rest[5:].strip()
                    try:
                        from charon.devop.devop_runtime import operation_dir, append_operation_event, set_operation_status
                        op_path = operation_dir(common.STATE_DIR, op_id) / 'operation.json'
                        if not op_path.exists():
                            common.emit({'type': 'error', 'error': f'No software-dev operation found: {op_id}', 'request_id': request_id})
                            return
                        op_doc = common._load_json(op_path, {})
                        op_doc['stop_requested'] = True
                        op_doc['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                        op_path.write_text(json.dumps(op_doc, indent=2, ensure_ascii=False))
                        append_operation_event(common.STATE_DIR, op_id, 'stop_requested', summary='User requested stop.')
                        set_operation_status(common.STATE_DIR, op_id, 'stopping', 'User requested stop.')
                        common.emit({'type': 'status', 'message': f'Requested stop for software-dev operation {op_id}', 'request_id': request_id})
                        common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                    except Exception as e:
                        common.emit({'type': 'error', 'error': f'Software-dev stop failed: {e}', 'request_id': request_id})
                    return
                try:
                    from charon.devop.devop_agents import start_autonomous_software_operation
                    res = start_autonomous_software_operation(
                        common.STATE_DIR,
                        Path(self._devop_project_root()),
                        prompt=rest,
                        parent_agent_id=self._active_agent_id or '',
                    )
                    op = res.get('operation') or {}
                    coord = res.get('coordinator') or {}
                    common.emit({
                        'type': 'status',
                        'message': (
                            f'Started software-dev operation.\n'
                            f'Operation: {op.get("operation_id")}\n'
                            f'Coordinator: {coord.get("name") or coord.get("id") or "(starting)"}\n'
                            f'Use /devop status {op.get("operation_id")} to inspect progress.'
                        ),
                        'request_id': request_id,
                    })
                    common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Failed to start software-dev operation: {e}', 'request_id': request_id})
                return
            if command == '/monitor' or command.startswith('/monitor '):
                rest = command[8:].strip() if command.startswith('/monitor ') else ''
                if not rest:
                    common.emit({'type': 'status', 'message': 'Usage: /monitor every hour <url>', 'request_id': request_id})
                    return
                browser_mode = rest.lower().startswith('browser ')
                if browser_mode:
                    rest = rest[8:].strip()
                interval = _parse_interval_phrase(rest)
                url_match = re.search(r'(https?://\S+)', rest)
                if interval <= 0 or not url_match:
                    common.emit({'type': 'error', 'error': 'Usage: /monitor every hour <url>', 'request_id': request_id})
                    return
                url = url_match.group(1).rstrip('.,)')
                expect_match = re.search(r'\b(?:expect|contains?)\s+"([^"]+)"', rest, re.I)
                expected_text = expect_match.group(1).strip() if expect_match else ''
                prefix = '/automate browser' if browser_mode else '/automate'
                self.handle_command(f'{prefix} every {interval} seconds check {url}' + (f' expect "{expected_text}"' if expected_text else ''), request_id)
                return
            if command == '/automate' or command.startswith('/automate '):
                rest = command[10:].strip() if command.startswith('/automate ') else ''
                if not rest:
                    common.emit({'type': 'status', 'message': 'Usage: /automate every <n> <unit> check <url>', 'request_id': request_id})
                    return
                browser_mode = False
                if rest.lower().startswith('browser '):
                    browser_mode = True
                    rest = rest[8:].strip()
                if rest == 'list' or rest.startswith('list '):
                    filter_mode = rest[5:].strip().lower() if rest.startswith('list ') else ''
                    try:
                        from charon.automation.automation_runtime import list_automations
                        items = list_automations(common.STATE_DIR)
                        if filter_mode == 'cron':
                            items = [a for a in items if str((a.get('schedule') or {}).get('type') or '').lower() == 'cron']
                        elif filter_mode == 'continuous':
                            items = [a for a in items if str(a.get('mode') or '').lower() == 'continuous']
                        elif filter_mode == 'scheduled':
                            items = [a for a in items if str(a.get('mode') or '').lower() == 'scheduled']
                        if not items:
                            common.emit({'type': 'status', 'message': 'No automations found.' if not filter_mode else f'No {filter_mode} automations found.', 'request_id': request_id})
                            return
                        lines = ['Automations:']
                        for a in items[:40]:
                            sched = a.get('schedule') or {}
                            sched_desc = ''
                            if str(a.get('mode') or '') == 'continuous':
                                sched_desc = f'continuous/{sched.get("poll_seconds") or (a.get("execution_policy") or {}).get("poll_seconds") or 60}s'
                            elif str(sched.get('type') or '') == 'cron':
                                sched_desc = f'cron {sched.get("cron")}'
                            else:
                                sched_desc = f'every {sched.get("interval_seconds") or 0}s'
                            lines.append(
                                f'- {a.get("automation_id")} | {a.get("title")} | '
                                f'{a.get("status")}/{a.get("health")} | {sched_desc} | '
                                f'next={a.get("next_run_at") or "continuous"}'
                            )
                        common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                    except Exception as e:
                        common.emit({'type': 'error', 'error': f'Automation list failed: {e}', 'request_id': request_id})
                    return
                if rest.startswith('status '):
                    automation_id = rest[7:].strip()
                    try:
                        from charon.automation.automation_runtime import get_automation_state
                        doc = get_automation_state(common.STATE_DIR, automation_id)
                        if not doc:
                            common.emit({'type': 'error', 'error': f'No automation found: {automation_id}', 'request_id': request_id})
                            return
                        lines = [
                            f'Automation: {doc.get("automation_id")}',
                            f'Title: {doc.get("title")}',
                            f'Status: {doc.get("status")}',
                            f'Health: {doc.get("health")}',
                            f'Next run: {doc.get("next_run_at") or "-"}',
                            f'Last result: {doc.get("last_result_summary") or "-"}',
                        ]
                        for run in doc.get('runs_tail') or []:
                            lines.append(f'- {run.get("ts")} [{"ok" if run.get("ok") else "fail"}] {run.get("summary")}')
                        common.emit({'type': 'status', 'message': '\n'.join(lines[:16]), 'request_id': request_id})
                    except Exception as e:
                        common.emit({'type': 'error', 'error': f'Automation status failed: {e}', 'request_id': request_id})
                    return
                if rest.startswith('webhook '):
                    body = rest[8:].strip()
                    parts = body.split(None, 1)
                    if len(parts) < 2:
                        common.emit({'type': 'error', 'error': 'Usage: /automate webhook <automation_id> <url>', 'request_id': request_id})
                        return
                    automation_id, webhook_url = parts[0].strip(), parts[1].strip()
                    try:
                        from charon.automation.automation_runtime import set_automation_webhook
                        doc = set_automation_webhook(common.STATE_DIR, automation_id, webhook_url)
                        if not doc:
                            common.emit({'type': 'error', 'error': f'No automation found: {automation_id}', 'request_id': request_id})
                            return
                        common.emit({'type': 'status', 'message': f'Updated webhook for automation {automation_id}', 'request_id': request_id})
                        common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                    except Exception as e:
                        common.emit({'type': 'error', 'error': f'Automation webhook update failed: {e}', 'request_id': request_id})
                    return
                for action_name, _fn_name in [('pause', 'pause_automation'), ('resume', 'resume_automation'), ('stop', 'request_stop_automation')]:
                    if rest.startswith(action_name + ' '):
                        automation_id = rest[len(action_name) + 1:].strip()
                        try:
                            from charon.automation.automation_runtime import pause_automation, resume_automation, request_stop_automation
                            fn = {'pause': pause_automation, 'resume': resume_automation, 'stop': request_stop_automation}[action_name]
                            doc = fn(common.STATE_DIR, automation_id)
                            if not doc:
                                common.emit({'type': 'error', 'error': f'No automation found: {automation_id}', 'request_id': request_id})
                                return
                            common.emit({'type': 'status', 'message': f'{action_name.capitalize()}d automation {automation_id}', 'request_id': request_id})
                            common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                        except Exception as e:
                            common.emit({'type': 'error', 'error': f'Automation {action_name} failed: {e}', 'request_id': request_id})
                        return
                workflow_mode = False
                if rest.lower().startswith('browser-workflow '):
                    workflow_mode = True
                    rest = rest[17:].strip()
                interval = _parse_interval_phrase(rest)
                sec_match = re.search(r'\bevery\s+(\d+)\s+seconds?\b', rest, re.I)
                if sec_match:
                    interval = int(sec_match.group(1))
                cron_match = re.search(r'^cron\s+"([^"]+)"\s+check\s+', rest, re.I)
                continuous_match = re.search(r'^continuous(?:\s+every\s+(\d+)\s+seconds?)?\s+check\s+', rest, re.I)
                workflow_steps_match = re.search(r'\bsteps\s+(.+)$', rest, re.I)
                workflow_file_match = re.search(r'\bfrom\s+([^\s]+)\s*$', rest, re.I)
                if workflow_mode:
                    mode = 'scheduled'
                    schedule = {}
                    if cron_match:
                        schedule = {'type': 'cron', 'cron': cron_match.group(1).strip()}
                    elif continuous_match:
                        mode = 'continuous'
                        poll_seconds = int(continuous_match.group(1) or 60)
                        schedule = {'type': 'continuous', 'poll_seconds': poll_seconds}
                    else:
                        if interval <= 0:
                            common.emit({'type': 'error', 'error': 'Usage: /automate browser-workflow every <n> <unit> steps <json> | from <file>', 'request_id': request_id})
                            return
                        schedule = {'type': 'interval', 'interval_seconds': interval}
                    steps = None
                    workflow_source = ''
                    if workflow_steps_match:
                        raw_steps = workflow_steps_match.group(1).strip()
                        try:
                            parsed = json.loads(raw_steps)
                        except Exception as e:
                            common.emit({'type': 'error', 'error': f'Invalid workflow JSON: {e}', 'request_id': request_id})
                            return
                        steps = parsed if isinstance(parsed, list) else None
                        workflow_source = 'inline-json'
                    elif workflow_file_match:
                        workflow_path = workflow_file_match.group(1).strip().strip('"').strip("'")
                        steps = _load_workflow_steps_spec(Path(self._devop_project_root()), workflow_path)
                        workflow_source = workflow_path
                    if not isinstance(steps, list) or not steps:
                        common.emit({'type': 'error', 'error': 'Workflow steps must be a non-empty JSON list. Use steps <json> or from <file>.', 'request_id': request_id})
                        return
                    title = 'Browser workflow automation'
                    first_url = ''
                    for step in steps:
                        if isinstance(step, dict) and step.get('url'):
                            first_url = str(step.get('url') or '')
                            break
                    try:
                        from charon.automation.automation_runtime import create_automation
                        doc = create_automation(
                            common.STATE_DIR,
                            Path(self._devop_project_root()),
                            title=title if not first_url else f'Browser workflow: {first_url}',
                            goal=rest,
                            kind='browser_workflow',
                            mode=mode,
                            schedule=schedule,
                            action={'steps': steps, 'screenshot_on_failure': True, 'workflow_source': workflow_source},
                            created_by_agent_id=self._active_agent_id or '',
                            operation_role='monitor',
                        )
                        common.emit({'type': 'status', 'message': f'Started browser workflow automation {doc.get("automation_id")}\nMode: {mode}\nSource: {workflow_source or "inline-json"}\nNext run: {doc.get("next_run_at") or "continuous"}', 'request_id': request_id})
                        common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                    except Exception as e:
                        common.emit({'type': 'error', 'error': f'Failed to create browser workflow automation: {e}', 'request_id': request_id})
                    return
                url_match = re.search(r'(https?://\S+)', rest)
                if not url_match or 'check' not in rest.lower():
                    common.emit({'type': 'error', 'error': 'Usage: /automate every <n> <unit> check <url>', 'request_id': request_id})
                    return
                url = url_match.group(1).rstrip('.,)')
                expect_match = re.search(r'\bexpect\s+"([^"]+)"', rest, re.I)
                expected_text = expect_match.group(1).strip() if expect_match else ''
                mode = 'scheduled'
                schedule = {}
                if cron_match:
                    schedule = {'type': 'cron', 'cron': cron_match.group(1).strip()}
                elif continuous_match:
                    mode = 'continuous'
                    poll_seconds = int(continuous_match.group(1) or 60)
                    schedule = {'type': 'continuous', 'poll_seconds': poll_seconds}
                else:
                    if interval <= 0:
                        common.emit({'type': 'error', 'error': 'Usage: /automate every <n> <unit> check <url>', 'request_id': request_id})
                        return
                    schedule = {'type': 'interval', 'interval_seconds': interval}
                try:
                    from charon.automation.automation_runtime import create_automation
                    doc = create_automation(
                        common.STATE_DIR,
                        Path(self._devop_project_root()),
                        title=(f'Browser monitor: {url}' if browser_mode else f'Website monitor: {url}'),
                        goal=rest,
                        kind=('browser_check' if browser_mode else 'http_check'),
                        mode=mode,
                        schedule=schedule,
                        action={'url': url, 'method': 'GET', 'timeout_seconds': 20, 'expected_text': expected_text, 'screenshot_on_failure': True},
                        created_by_agent_id=self._active_agent_id or '',
                        operation_role='monitor',
                    )
                    common.emit({'type': 'status', 'message': f'Started automation {doc.get("automation_id")} for {url}\nMode: {mode}\nNext run: {doc.get("next_run_at") or "continuous"}', 'request_id': request_id})
                    common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Failed to create automation: {e}', 'request_id': request_id})
                return
            if command == '/add-remote' or command.startswith('/add-remote '):
                rest = command[12:].strip() if command.startswith('/add-remote ') else ''
                try:
                    from charon.fleet.remote_onboard import test_ssh, check_boat_installed, deploy_boat_remote, discover_remote_agents, auto_configure_fleet

                    if not rest:
                        common.emit({'type': 'status', 'message': (
                            'Usage: /add-remote <host> [user]\n'
                            '  /add-remote 55.55.55.55 ubuntu    — test SSH and discover agents\n'
                            '  /add-remote confirm                — save discovered config\n'
                            '  /add-remote cancel                 — discard pending onboarding'
                        ), 'request_id': request_id})
                        return

                    if rest == 'cancel':
                        self._pending_remote_onboard = None
                        common.emit({'type': 'status', 'message': 'Remote onboarding cancelled.', 'request_id': request_id})
                        return

                    if rest == 'confirm':
                        pending = self._pending_remote_onboard
                        if not pending:
                            common.emit({'type': 'error', 'error': 'No pending remote onboard. Run /add-remote <host> first.', 'request_id': request_id})
                            return
                        all_agents = pending.get('agents', [])
                        if not all_agents:
                            common.emit({'type': 'error', 'error': 'No agents to add. Run /add-remote <host> to discover agents first.', 'request_id': request_id})
                            return
                        server = auto_configure_fleet(
                            host=pending['host'],
                            user=pending.get('user', ''),
                            agents=all_agents,
                        )
                        self._pending_remote_onboard = None
                        # Start fleet sync if not already running
                        try:
                            from charon.fleet.fleet_sync import start_fleet_sync
                            start_fleet_sync()
                        except Exception as exc:
                            _diag('commands_mixin', 'fleet sync auto-start failed after remote onboard', error=exc)
                        common.emit({'type': 'status', 'message': (
                            f'Added server "{server["id"]}" ({server["host"]}) with '
                            f'{len(server.get("agents", []))} agent(s) to fleet.\n'
                            f'Remote sessions will appear in F3 grid shortly.'
                        ), 'request_id': request_id})
                        common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                        return

                    # Parse host and optional user
                    parts = rest.split()
                    host = parts[0]
                    user = parts[1] if len(parts) > 1 else ''

                    # Step 1: Test SSH
                    common.emit({'type': 'status', 'message': f'Testing SSH to {user + "@" if user else ""}{host}...', 'request_id': request_id})
                    ssh_ok, ssh_msg = test_ssh(host, user)
                    common.emit({'type': 'status', 'message': ssh_msg, 'request_id': request_id})
                    if not ssh_ok:
                        if not user:
                            common.emit({'type': 'status', 'message': 'Tip: try with a username: /add-remote <host> <user>', 'request_id': request_id})
                        return

                    # Step 2: Check boat
                    common.emit({'type': 'status', 'message': 'Checking for charons-boat on remote...', 'request_id': request_id})
                    boat_ok, boat_msg = check_boat_installed(host, user)
                    if boat_ok:
                        common.emit({'type': 'status', 'message': f'charons-boat found: {boat_msg}', 'request_id': request_id})
                    else:
                        common.emit({'type': 'status', 'message': 'charons-boat not found. Deploying...', 'request_id': request_id})
                        dep_ok, dep_msg = deploy_boat_remote(host, user)
                        if dep_ok:
                            common.emit({'type': 'status', 'message': f'Deployed: {dep_msg}', 'request_id': request_id})
                        else:
                            common.emit({'type': 'status', 'message': f'Deploy failed: {dep_msg}\nContinuing with tmux discovery...', 'request_id': request_id})

                    # Step 3: Discover agents
                    common.emit({'type': 'status', 'message': 'Discovering agents...', 'request_id': request_id})
                    discovery = discover_remote_agents(host, user)
                    boat_agents = discovery.get('boat_sessions', [])
                    tmux_agents = discovery.get('tmux_agents', [])
                    tmux_other = discovery.get('tmux_other', [])
                    all_agents = boat_agents + tmux_agents

                    if not all_agents and not tmux_other:
                        common.emit({'type': 'status', 'message': 'No agents found on remote. You can still add the server manually.', 'request_id': request_id})
                        # Save pending with empty agents so user can still confirm (creates empty server entry)
                        self._pending_remote_onboard = {'host': host, 'user': user, 'agents': []}
                        return

                    lines = [f'Found {len(all_agents)} agent(s) on {host}:']
                    for i, a in enumerate(all_agents, 1):
                        source = 'boat-wrapped' if a.get('source') == 'boat' else 'tmux session'
                        lines.append(f'  {i}. {a["name"]} ({a["type"]}) — {source}, {a["status"]}')
                    if tmux_other:
                        lines.append(f'\nAlso found {len(tmux_other)} other tmux session(s):')
                        for a in tmux_other:
                            lines.append(f'  - {a["name"]} (unrecognized agent)')
                    if tmux_agents:
                        lines.append('\nTmux agents will be auto-bridged via boat\'s tmux discovery.')
                    lines.append('\nType /add-remote confirm to save, or /add-remote cancel to abort.')

                    self._pending_remote_onboard = {
                        'host': host,
                        'user': user,
                        'agents': all_agents,
                        'discovery': discovery,
                    }
                    common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Remote onboarding failed: {e}', 'request_id': request_id})
                return

            if command == '/project' or command.startswith('/project '):
                rest = command[8:].strip() if command.startswith('/project ') else ''
                try:
                    onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                    registry = _load_project_registry()
                    if not rest or rest == 'list':
                        if not registry:
                            common.emit({'type': 'status', 'message': 'No explicit projects yet. Use /project create <name> [path]', 'request_id': request_id})
                        else:
                            lines = ['Explicit projects:']
                            current = str(onboarding.get('project') or '').strip()
                            for p in registry[:20]:
                                name = str(p.get('name') or '')
                                path = str(p.get('path') or '')
                                mark = '*' if current and path == current else ' '
                                lines.append(f' {mark} {name} — {path}')
                            common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                        common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                        return
                    if rest.startswith('create '):
                        body = rest[7:].strip()
                        if not body:
                            common.emit({'type': 'error', 'error': 'Usage: /project create <name> [path]', 'request_id': request_id})
                            return
                        parts = body.split(None, 1)
                        name = parts[0].strip()
                        path = parts[1].strip() if len(parts) > 1 else str(onboarding.get('project') or str(common.ROOT)).strip()
                        slug = _project_slug(name)
                        display_name = name.replace('-', ' ').replace('_', ' ').strip() or slug
                        existing = next((p for p in registry if str(p.get('name') or '').strip() == display_name or _project_slug(p.get('name') or '') == slug), None)
                        if existing is None:
                            registry.append({
                                'id': f'project-{slug}',
                                'name': display_name,
                                'slug': slug,
                                'path': path,
                                'description': '',
                                'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                            })
                            _save_project_registry(registry)
                            common.emit({'type': 'status', 'message': f'Created project {display_name} at {path}', 'request_id': request_id})
                        else:
                            common.emit({'type': 'status', 'message': f'Project already exists: {existing.get("name", display_name)}', 'request_id': request_id})
                        common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                        return
                    if rest.startswith('use '):
                        choice = rest[4:].strip()
                        target = next((p for p in registry if str(p.get('name') or '').strip() == choice or str(p.get('id') or '').strip() == choice or str(p.get('slug') or '').strip() == _project_slug(choice)), None)
                        if not target:
                            common.emit({'type': 'error', 'error': f'Unknown project: {choice}', 'request_id': request_id})
                            return
                        onboarding['project'] = str(target.get('path') or '').strip() or str(common.ROOT)
                        (common.STATE_DIR / 'onboarding.json').write_text(json.dumps(onboarding, indent=2, ensure_ascii=False))
                        common.emit({'type': 'status', 'message': f'Selected project {target.get("name", "project")}', 'request_id': request_id})
                        common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                        return
                    common.emit({'type': 'error', 'error': 'Usage: /project [list|create|use]', 'request_id': request_id})
                    return
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Project command failed: {e}', 'request_id': request_id})
                    return

            if command in ('/hermes', '/pi'):
                try:
                    from charon.fleet.external_session_launcher import launch_wrapped_session
                    onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(common.ROOT)).strip()
                    agent_kind = command[1:]
                    result = launch_wrapped_session(
                        state_dir=common.STATE_DIR,
                        project_root=project,
                        agent_kind=agent_kind,
                    )
                    if result.get('ok'):
                        display_name = str(result.get('display_name') or agent_kind)
                        session_name = str(result.get('tmux_session') or result.get('session_name') or '')
                        self._register_owned_boat_session(session_name)
                        common.emit({'type': 'status', 'message': f'✓ {display_name} session created: {session_name}', 'request_id': request_id})
                        common.emit({'type': 'status', 'message': 'To view or interact with it, press F3 for the sessions grid.', 'request_id': request_id})
                        self.handle_refresh(request_id)
                    else:
                        common.emit({'type': 'error', 'error': f'Failed to create {agent_kind} session: {result.get("error", "unknown error")}', 'request_id': request_id})
                    return
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Failed to create external session: {e}', 'request_id': request_id})
                    return

            if command == '/conversation' or command.startswith('/conversation '):
                rest = command[13:].strip() if command.startswith('/conversation ') else ''
                try:
                    if not rest:
                        common.emit({'type': 'status', 'message': 'Usage: /conversation <agent-type> [peer|teacher student|debate|researcher reviewer|strategist critic|planner critic|architect reviewer|optimist skeptic|pair-programmers|dialogue|<count>] <topic>', 'request_id': request_id})
                        return
                    parts = rest.split()
                    provider = (parts[0] if parts else '').strip().lower()
                    from charon.conversation.conversation_participants import get_conversation_adapter
                    adapter = get_conversation_adapter(provider)
                    if not adapter:
                        common.emit({'type': 'error', 'error': f'Unsupported conversation provider for now: {provider}', 'request_id': request_id})
                        return
                    if not adapter.capabilities.can_spawn:
                        common.emit({'type': 'error', 'error': f'Conversation spawning is not wired yet for: {provider}', 'request_id': request_id})
                        return
                    roles: list[str] = []
                    topic = ''
                    runner_mode = 'teacher-student'
                    if len(parts) >= 4 and parts[1].lower() == 'teacher' and parts[2].lower() == 'student':
                        roles = ['teacher', 'student']
                        topic = ' '.join(parts[3:]).strip()
                        runner_mode = 'teacher-student'
                    elif len(parts) >= 3 and parts[1].lower() in ('peer', 'dialogue', 'discuss'):
                        roles = ['peer-1', 'peer-2']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'peer'
                    elif len(parts) >= 4 and parts[1].lower() == 'researcher' and parts[2].lower() == 'reviewer':
                        roles = ['researcher', 'reviewer']
                        topic = ' '.join(parts[3:]).strip()
                        runner_mode = 'researcher-reviewer'
                    elif len(parts) >= 3 and parts[1].lower() in ('research', 'researcher-reviewer'):
                        roles = ['researcher', 'reviewer']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'researcher-reviewer'
                    elif len(parts) >= 3 and parts[1].lower() in ('pair-programmers', 'pair-programming', 'pair-programmer'):
                        roles = ['driver', 'navigator']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'pair-programmers'
                    elif len(parts) >= 4 and parts[1].lower() == 'pair' and parts[2].lower() in ('programmers', 'programming'):
                        roles = ['driver', 'navigator']
                        topic = ' '.join(parts[3:]).strip()
                        runner_mode = 'pair-programmers'
                    elif len(parts) >= 4 and parts[1].lower() == 'strategist' and parts[2].lower() == 'critic':
                        roles = ['strategist', 'critic']
                        topic = ' '.join(parts[3:]).strip()
                        runner_mode = 'strategist-critic'
                    elif len(parts) >= 3 and parts[1].lower() in ('strategist-critic', 'strategy-critique'):
                        roles = ['strategist', 'critic']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'strategist-critic'
                    elif len(parts) >= 4 and parts[1].lower() == 'planner' and parts[2].lower() == 'critic':
                        roles = ['planner', 'critic']
                        topic = ' '.join(parts[3:]).strip()
                        runner_mode = 'planner-critic'
                    elif len(parts) >= 3 and parts[1].lower() in ('planner-critic', 'planning-critique'):
                        roles = ['planner', 'critic']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'planner-critic'
                    elif len(parts) >= 4 and parts[1].lower() == 'architect' and parts[2].lower() == 'reviewer':
                        roles = ['architect', 'reviewer']
                        topic = ' '.join(parts[3:]).strip()
                        runner_mode = 'architect-reviewer'
                    elif len(parts) >= 3 and parts[1].lower() in ('architect-reviewer', 'architecture-review'):
                        roles = ['architect', 'reviewer']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'architect-reviewer'
                    elif len(parts) >= 4 and parts[1].lower() == 'optimist' and parts[2].lower() == 'skeptic':
                        roles = ['optimist', 'skeptic']
                        topic = ' '.join(parts[3:]).strip()
                        runner_mode = 'optimist-skeptic'
                    elif len(parts) >= 3 and parts[1].lower() in ('optimist-skeptic', 'optimism-skepticism'):
                        roles = ['optimist', 'skeptic']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'optimist-skeptic'
                    elif len(parts) >= 3 and parts[1].lower() in ('debate',):
                        roles = ['advocate', 'opposition']
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'debate'
                    elif len(parts) >= 3 and str(parts[1]).isdigit():
                        count = max(2, int(parts[1]))
                        roles = ['peer-1', 'peer-2'] if count == 2 else [f'peer-{idx+1}' for idx in range(count)]
                        topic = ' '.join(parts[2:]).strip()
                        runner_mode = 'peer'
                    else:
                        roles = ['teacher', 'student']
                        topic = ' '.join(parts[1:]).strip()
                        runner_mode = 'teacher-student'
                    topic = topic or 'open discussion'
                    onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(common.ROOT)).strip()
                    if runner_mode == 'dialogue':
                        participants = [
                            {'id': f'hermes-{idx+1}', 'role': role, 'name': f'Hermes {idx+1}'}
                            for idx, role in enumerate(roles)
                        ]
                    else:
                        participants = [
                            {'id': f'hermes-{idx+1}', 'role': role, 'name': f'Hermes {idx+1}'}
                            for idx, role in enumerate(roles)
                        ]
                    self._create_conversation_room(
                        agent_type=provider,
                        kind='conversation',
                        title=topic,
                        project=project,
                        participants=participants,
                        meta={'provider': provider, 'count': len(participants), 'topic': topic, 'conversation_mode': runner_mode},
                        request_id=request_id,
                        start_runner=True,
                        runner_mode=runner_mode,
                    )
                    return
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Conversation command failed: {e}', 'request_id': request_id})
                    return

            if command == '/team' or command.startswith('/team '):
                rest = command[5:].strip() if command.startswith('/team ') else ''
                try:
                    if not rest:
                        common.emit({'type': 'status', 'message': 'Usage: /team <agent-type> <count> <topic>', 'request_id': request_id})
                        return
                    parts = rest.split(None, 2)
                    provider = (parts[0] if len(parts) > 0 else '').strip().lower()
                    count = int(parts[1]) if len(parts) > 1 and str(parts[1]).isdigit() else 2
                    topic = (parts[2] if len(parts) > 2 else '').strip() or 'open discussion'
                    from charon.conversation.conversation_participants import get_conversation_adapter
                    adapter = get_conversation_adapter(provider)
                    if not adapter:
                        common.emit({'type': 'error', 'error': f'Unsupported team provider for now: {provider}', 'request_id': request_id})
                        return
                    if not adapter.capabilities.can_spawn:
                        common.emit({'type': 'error', 'error': f'Team spawning is not wired yet for: {provider}', 'request_id': request_id})
                        return

                    onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(common.ROOT)).strip()
                    participants = []
                    for idx in range(count):
                        role = f'peer-{idx+1}' if count > 2 else ('peer-1' if idx == 0 else 'peer-2')
                        participants.append({'id': f'hermes-{idx+1}', 'role': role, 'name': f'Hermes {idx+1}'})
                    self._create_conversation_room(
                        agent_type=provider,
                        kind='conversation',
                        title=topic,
                        project=project,
                        participants=participants,
                        meta={'provider': provider, 'count': count, 'topic': topic, 'conversation_mode': 'peer'},
                        request_id=request_id,
                        start_runner=True,
                        runner_mode='peer',
                    )
                    return
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Team command failed: {e}', 'request_id': request_id})
                    return

            if command == '/devteam' or command.startswith('/devteam '):
                rest = command[8:].strip() if command.startswith('/devteam ') else ''
                try:
                    if not rest:
                        common.emit({'type': 'status', 'message': 'Usage: /devteam <agent-type> <count> <goal>', 'request_id': request_id})
                        return
                    parts = rest.split(None, 2)
                    provider = (parts[0] if len(parts) > 0 else '').strip().lower()
                    count = int(parts[1]) if len(parts) > 1 and str(parts[1]).isdigit() else 2
                    goal = (parts[2] if len(parts) > 2 else '').strip() or 'engineering task'
                    from charon.conversation.conversation_participants import get_conversation_adapter
                    adapter = get_conversation_adapter(provider)
                    if not adapter:
                        common.emit({'type': 'error', 'error': f'Unsupported devteam provider for now: {provider}', 'request_id': request_id})
                        return
                    if not adapter.capabilities.can_spawn:
                        common.emit({'type': 'error', 'error': f'Devteam spawning is not wired yet for: {provider}', 'request_id': request_id})
                        return
                    onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(common.ROOT)).strip()
                    participants = [
                        {'id': f'hermes-{idx+1}', 'role': 'developer', 'name': f'Hermes {idx+1}'}
                        for idx in range(count)
                    ]
                    self._create_conversation_room(
                        agent_type=provider,
                        kind='devteam',
                        title=goal,
                        project=project,
                        participants=participants,
                        meta={'provider': provider, 'count': count, 'goal': goal, 'team_mode': 'devteam'},
                        request_id=request_id,
                        start_runner=False,
                    )
                    return
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Devteam command failed: {e}', 'request_id': request_id})
                    return

            if command == '/pause-room' or command.startswith('/pause-room '):
                room_id = command[12:].strip() if command.startswith('/pause-room ') else ''
                try:
                    if not room_id:
                        common.emit({'type': 'status', 'message': 'Usage: /pause-room <room-id>', 'request_id': request_id})
                        return
                    from charon.agents.inter_agent_rooms import append_event, load_room, update_room
                    room = load_room(common.STATE_DIR, room_id)
                    if not room:
                        common.emit({'type': 'error', 'error': f'Unknown room: {room_id}', 'request_id': request_id})
                        return
                    update_room(common.STATE_DIR, room_id, status='paused', summary=f'Paused room {room_id}')
                    append_event(common.STATE_DIR, room_id, {'type': 'room_paused', 'summary': f'Paused room {room_id}'})
                    common.emit({'type': 'status', 'message': f'Paused room: {room_id}', 'request_id': request_id})
                    self.handle_refresh(request_id)
                    return
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Pause room failed: {e}', 'request_id': request_id})
                    return

            if command == '/resume-room' or command.startswith('/resume-room '):
                room_id = command[13:].strip() if command.startswith('/resume-room ') else ''
                try:
                    if not room_id:
                        common.emit({'type': 'status', 'message': 'Usage: /resume-room <room-id>', 'request_id': request_id})
                        return
                    from charon.agents.inter_agent_rooms import append_event, load_room, update_room
                    room = load_room(common.STATE_DIR, room_id)
                    if not room:
                        common.emit({'type': 'error', 'error': f'Unknown room: {room_id}', 'request_id': request_id})
                        return
                    update_room(common.STATE_DIR, room_id, status='active', summary=f'Resumed room {room_id}')
                    append_event(common.STATE_DIR, room_id, {'type': 'room_resumed', 'summary': f'Resumed room {room_id}'})
                    participants = list(room.get('participants') or [])
                    if len(participants) >= 2:
                        self._start_conversation_room_runner(
                            room_id,
                            str(room.get('title') or room_id),
                            participants,
                            mode=self._room_runner_mode(room),
                        )
                    common.emit({'type': 'status', 'message': f'Resumed room: {room_id}', 'request_id': request_id})
                    self.handle_refresh(request_id)
                    return
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Resume room failed: {e}', 'request_id': request_id})
                    return

            if command == '/say-room' or command.startswith('/say-room '):
                rest = command[10:].strip() if command.startswith('/say-room ') else ''
                try:
                    if not rest:
                        common.emit({'type': 'status', 'message': 'Usage: /say-room <room-id> <message>', 'request_id': request_id})
                        return
                    parts = shlex.split(rest)
                    if len(parts) < 2:
                        common.emit({'type': 'status', 'message': 'Usage: /say-room <room-id> <message>', 'request_id': request_id})
                        return
                    room_id = parts[0]
                    message = ' '.join(parts[1:]).strip()
                    if self._dispatch_libris_room_intervention(room_id, target='whole', when='now', message=message, request_id=request_id, mode='say'):
                        return
                    from charon.agents.inter_agent_rooms import append_event, load_room, queue_injection
                    room = load_room(common.STATE_DIR, room_id)
                    if not room:
                        common.emit({'type': 'error', 'error': f'Unknown room: {room_id}', 'request_id': request_id})
                        return
                    item = queue_injection(common.STATE_DIR, room_id, message=message, target='whole', when='now', sender='user')
                    if not item:
                        common.emit({'type': 'error', 'error': f'Failed to send room message for: {room_id}', 'request_id': request_id})
                        return
                    append_event(common.STATE_DIR, room_id, {
                        'type': 'room_message_sent',
                        'target': 'whole',
                        'summary': message[:240],
                        'message': message,
                    })
                    if str(room.get('status') or 'active') == 'active' and len(list(room.get('participants') or [])) >= 2:
                        self._start_conversation_room_runner(
                            room_id,
                            str(room.get('title') or room_id),
                            list(room.get('participants') or []),
                            mode=self._room_runner_mode(room),
                        )
                    common.emit({'type': 'status', 'message': f'Sent room message to {room_id}: {message[:120]}', 'request_id': request_id})
                    self.handle_refresh(request_id)
                    return
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Say room failed: {e}', 'request_id': request_id})
                    return

            if command == '/inject-room' or command.startswith('/inject-room '):
                rest = command[13:].strip() if command.startswith('/inject-room ') else ''
                try:
                    if not rest:
                        common.emit({'type': 'status', 'message': 'Usage: /inject-room <room-id> [--target whole|teacher|student|<participant>|coordinator|topic:<slug>|node:<agent-id>|researcher:<slug>|judge:<slug>|shade:<agent-id>] [--when now|next] <message>', 'request_id': request_id})
                        return
                    parts = shlex.split(rest)
                    if not parts:
                        common.emit({'type': 'status', 'message': 'Usage: /inject-room <room-id> [--target whole|teacher|student|<participant>|coordinator|topic:<slug>|node:<agent-id>|researcher:<slug>|judge:<slug>|shade:<agent-id>] [--when now|next] <message>', 'request_id': request_id})
                        return
                    room_id = parts[0]
                    target = 'whole'
                    when = 'next'
                    idx = 1
                    while idx < len(parts):
                        token = parts[idx]
                        if token == '--target' and idx + 1 < len(parts):
                            target = parts[idx + 1]
                            idx += 2
                            continue
                        if token == '--when' and idx + 1 < len(parts):
                            when = parts[idx + 1]
                            idx += 2
                            continue
                        break
                    message = ' '.join(parts[idx:]).strip()
                    if not message:
                        common.emit({'type': 'error', 'error': 'Injection message cannot be empty.', 'request_id': request_id})
                        return
                    if self._dispatch_libris_room_intervention(room_id, target=target, when=when, message=message, request_id=request_id, mode='inject'):
                        return
                    from charon.agents.inter_agent_rooms import append_event, load_room, queue_injection
                    room = load_room(common.STATE_DIR, room_id)
                    if not room:
                        common.emit({'type': 'error', 'error': f'Unknown room: {room_id}', 'request_id': request_id})
                        return
                    item = queue_injection(common.STATE_DIR, room_id, message=message, target=target, when=when, sender='user')
                    if not item:
                        common.emit({'type': 'error', 'error': f'Failed to queue injection for room: {room_id}', 'request_id': request_id})
                        return
                    append_event(common.STATE_DIR, room_id, {
                        'type': 'room_injection_requested',
                        'target': target,
                        'when': when,
                        'summary': message[:240],
                    })
                    if str(room.get('status') or 'active') == 'active' and len(list(room.get('participants') or [])) >= 2:
                        self._start_conversation_room_runner(
                            room_id,
                            str(room.get('title') or room_id),
                            list(room.get('participants') or []),
                            mode=self._room_runner_mode(room),
                        )
                    common.emit({'type': 'status', 'message': f'Queued room injection for {room_id} target={target} when={when}: {message[:120]}', 'request_id': request_id})
                    self.handle_refresh(request_id)
                    return
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Inject room failed: {e}', 'request_id': request_id})
                    return

            if command == '/delete-room' or command.startswith('/delete-room '):
                room_id = command[12:].strip() if command.startswith('/delete-room ') else ''
                try:
                    if not room_id:
                        common.emit({'type': 'status', 'message': 'Usage: /delete-room <room-id>', 'request_id': request_id})
                        return
                    from charon.agents.inter_agent_rooms import delete_room, load_room
                    room = load_room(common.STATE_DIR, room_id)
                    participant_sessions = list(room.get('participant_sessions') or []) if room else []
                    if not participant_sessions and room:
                        participant_sessions = [p.get('session') for p in (room.get('participants') or []) if p.get('session')]
                    terminated: list[str] = []
                    for session_name in participant_sessions:
                        if session_name and _terminate_boat_session(str(session_name)):
                            terminated.append(str(session_name))
                            self._owned_boat_sessions.discard(str(session_name))
                    if delete_room(common.STATE_DIR, room_id):
                        msg = f'Deleted room record: {room_id}'
                        if terminated:
                            msg += '\nClosed sessions: ' + ', '.join(terminated)
                        common.emit({'type': 'status', 'message': msg, 'request_id': request_id})
                        self.handle_refresh(request_id)
                    else:
                        common.emit({'type': 'error', 'error': f'Could not delete room record: {room_id}', 'request_id': request_id})
                    return
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Delete room failed: {e}', 'request_id': request_id})
                    return

            if command == '/resume' or command.startswith('/resume '):
                arg = command[8:].strip() if command.startswith('/resume ') else ''
                try:
                    from charon.conversation.conversation_store import list_conversations, load_conversation, dict_to_message, message_to_dict
                    convos = list_conversations(common.STATE_DIR)
                    if arg:
                        # Direct resume — must reset the engine so it gets the
                        # correct agent_id for the target session.
                        self._active_agent_id = arg
                        self.engine = None  # force re-creation with new agent_id
                        engine, _ = self._ensure_engine()
                        restored_count = 0
                        saved = None

                        # Try lossless store first — full raw history.
                        # Query with arg directly in case engine.agent_id differs.
                        store_msgs = _full_messages_from_store(arg)
                        if store_msgs:
                            restored_count = len(store_msgs)
                            if engine:
                                engine.messages = list(store_msgs)
                            saved = [message_to_dict(m) for m in store_msgs]

                        if not restored_count:
                            # Fall back to JSONL
                            saved = _sanitize_saved_messages(load_conversation(common.STATE_DIR, arg))
                            if saved and engine:
                                msgs = [dict_to_message(m) for m in saved]
                                engine.messages = msgs
                                # Migrate into lossless store
                                if engine.has_lossless_store:
                                    engine.import_into_store(msgs)

                        if saved:
                            self._load_tasks_from_ledger(arg)
                            common.emit({
                                'type': 'conversation_restored',
                                'messages': saved,
                                'count': len(saved),
                                'agent_id': arg,
                            })
                            # Push session info so task pane updates immediately
                            common.emit({
                                'type': 'refresh',
                                'payload': {'session_info': self._get_session_info()},
                            })
                        else:
                            common.emit({'type': 'error', 'error': f'No saved conversation for {arg}', 'request_id': request_id})
                    elif convos:
                        # Show session picker with last user message preview
                        # Pre-load SQLite counts + last user messages for accuracy
                        _store_info = {}
                        try:
                            from charon.infra.store_adapter import get_db
                            _sdb = get_db(common.STATE_DIR)
                            # Batch: get counts per agent
                            for row in _sdb.fetchall(
                                "SELECT agent_id, COUNT(*) as cnt FROM conversation_messages GROUP BY agent_id"
                            ):
                                _store_info[row['agent_id']] = {'count': row['cnt'], 'preview': ''}
                            # Batch: get last user message per agent (for preview)
                            for aid, info in _store_info.items():
                                row = _sdb.fetchone(
                                    "SELECT content FROM conversation_messages "
                                    "WHERE agent_id = ? AND role = 'user' AND content != '' "
                                    "ORDER BY seq DESC LIMIT 1",
                                    (aid,),
                                )
                                if row and row['content']:
                                    first_line = row['content'].strip().split('\n')[0]
                                    info['preview'] = first_line[:60] + ('…' if len(first_line) > 60 else '')
                        except Exception as exc:
                            _diag('commands_mixin', 'sqlite conversation preload failed; resume picker falls back to jsonl previews', error=exc)

                        items = []
                        for c in sorted(convos, key=lambda x: x.get('last_timestamp', 0), reverse=True):
                            age = ''
                            if c.get('last_timestamp'):
                                secs = time.time() - c['last_timestamp']
                                if secs < 60:
                                    age = f'{int(secs)}s ago'
                                elif secs < 3600:
                                    age = f'{int(secs/60)}m ago'
                                elif secs < 86400:
                                    age = f'{int(secs/3600)}h ago'
                                else:
                                    age = f'{int(secs/86400)}d ago'
                            # Use SQLite info when available and more complete
                            aid = c['agent_id']
                            si = _store_info.get(aid)
                            msg_count = c.get('message_count', 0)
                            preview = ''
                            if si and si['count'] >= msg_count:
                                msg_count = si['count']
                                preview = si.get('preview', '')
                            if not preview:
                                try:
                                    saved = load_conversation(common.STATE_DIR, aid)
                                    for msg in reversed(saved):
                                        if msg.get('role') == 'user' and msg.get('content', '').strip():
                                            first_line = msg['content'].strip().split('\n')[0]
                                            preview = first_line[:60]
                                            if len(first_line) > 60:
                                                preview += '…'
                                            break
                                except Exception:
                                    pass
                            items.append({
                                'id': aid,
                                'desc': f"{preview or '(no messages)'}",
                                'age': f"{msg_count}msg  {age}",
                            })
                        if items:
                            common.emit({
                                'type': 'model_picker',
                                'models': items,
                                'provider': 'resume',
                                'request_id': request_id,
                            })
                        else:
                            common.emit({'type': 'status', 'message': 'No other saved conversations found.', 'request_id': request_id})
                    else:
                        common.emit({'type': 'status', 'message': 'No saved conversations found.', 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Resume failed: {e}', 'request_id': request_id})
                return
            if command == '/hotkeys':
                common.emit({
                    'type': 'suggestions',
                    'title': 'Keyboard Shortcuts',
                    'items': [
                        {'cmd': 'F1', 'desc': 'Switch to Chat view'},
                        {'cmd': 'F2', 'desc': 'Switch to Dashboard view'},
                        {'cmd': 'F3', 'desc': 'Switch to Session Grid view'},
                        {'cmd': 'F4', 'desc': 'Switch to Room Controls view'},
                        {'cmd': 'd', 'desc': 'F4 Rooms: delete selected room and close its participant sessions'},
                        {'cmd': 'Tab', 'desc': 'Dashboard: switch agents/projects | Sessions: cycle panes'},
                        {'cmd': '↑↓', 'desc': 'Navigate lists, menus, grid'},
                        {'cmd': '←→', 'desc': 'Navigate session grid horizontally'},
                        {'cmd': 'Enter', 'desc': 'Select menu item / enter session / submit input'},
                        {'cmd': 'Escape', 'desc': 'Close menu / exit session'},
                        {'cmd': 'Ctrl+F', 'desc': 'Zoom/unzoom session in grid'},
                        {'cmd': 'Ctrl+T', 'desc': 'Toggle timestamps'},
                        {'cmd': 'Ctrl+Y', 'desc': 'Toggle visible thoughts'},
                        {'cmd': 'Ctrl+C', 'desc': 'Exit Charon'},
                        {'cmd': '/', 'desc': 'Open command menu'},
                    ],
                    'request_id': request_id,
                })
                return
            if command == '/timestamps':
                common.emit({'type': 'toggle_timestamps', 'request_id': request_id})
                return
            if command in ('/interrupt', '/abort'):
                self.handle_abort(request_id)
                return
            if command == '/thoughts':
                self.visible_thoughts = not self.visible_thoughts
                try:
                    settings = _load_ui_settings()
                    settings['visible_thoughts'] = self.visible_thoughts
                    _save_ui_settings(settings)
                except Exception as exc:
                    _diag('commands_mixin', 'ui settings save failed; visible-thoughts toggle not persisted', error=exc)
                common.emit({
                    'type': 'toggle_visible_thoughts',
                    'enabled': self.visible_thoughts,
                    'supported': self._thoughts_supported(),
                    'provider': self._current_provider_name(),
                    'request_id': request_id,
                })
                return
            if command.startswith('/setup shade-model '):
                model_name = command[18:].strip()
                if model_name == 'same':
                    from charon.providers.model_registry import load_registry, save_registry
                    reg = load_registry(common.STATE_DIR)
                    reg['shade_model_mode'] = 'same'
                    save_registry(common.STATE_DIR, reg)
                    common.emit({'type': 'status', 'message': 'Shade model: same as main agent.', 'request_id': request_id})
                elif model_name == 'auto':
                    from charon.providers.model_registry import load_registry, save_registry
                    reg = load_registry(common.STATE_DIR)
                    reg['shade_model_mode'] = 'auto'
                    save_registry(common.STATE_DIR, reg)
                    common.emit({'type': 'status', 'message': 'Shade model: auto (Charon picks per task).', 'request_id': request_id})
                else:
                    from charon.providers.model_registry import load_registry, save_registry
                    reg = load_registry(common.STATE_DIR)
                    reg['shade_model_mode'] = 'fixed'
                    # Parse provider/model format
                    if '/' in model_name:
                        parts = model_name.split('/', 1)
                        reg['shade_provider'] = parts[0]
                        reg['shade_model'] = parts[1]
                    else:
                        reg['shade_model'] = model_name
                    save_registry(common.STATE_DIR, reg)
                    common.emit({'type': 'status', 'message': f'Shade model: {model_name}', 'request_id': request_id})
                return
            if command.startswith('/setup shade-url '):
                url = command[17:].strip()
                from charon.providers.model_registry import load_registry, save_registry
                reg = load_registry(common.STATE_DIR)
                reg['shade_base_url'] = url
                save_registry(common.STATE_DIR, reg)
                common.emit({'type': 'status', 'message': f'Shade base URL: {url}', 'request_id': request_id})
                return
            if command.startswith('/setup shade-key '):
                key = command[17:].strip()
                from charon.providers.model_registry import load_registry, save_registry
                reg = load_registry(common.STATE_DIR)
                reg['shade_api_key'] = key
                save_registry(common.STATE_DIR, reg)
                common.emit({'type': 'status', 'message': 'Shade API key saved.', 'request_id': request_id})
                return
            if command == '/models' or command == '/models list':
                try:
                    import httpx
                    lines = []

                    # Local models (LM Studio / Ollama)
                    for name, url in [('LM Studio', 'http://127.0.0.1:1234/v1'), ('Ollama', 'http://127.0.0.1:11434/v1')]:
                        try:
                            resp = httpx.get(f'{url}/models', timeout=3)
                            if resp.status_code == 200:
                                data = resp.json()
                                models = [m.get('id', '?') for m in data.get('data', [])]
                                lines.append(f'{name} ({url}):')
                                for m in models:
                                    lines.append(f'  {m}')
                                lines.append('')
                        except Exception:
                            pass

                    # Current config
                    onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                    current = onboarding.get('model') or onboarding.get('provider_model') or 'none'
                    lines.append(f'Current model: {current}')
                    lines.append(f'Provider: {onboarding.get("provider", "none")}')

                    try:
                        from charon.providers.model_registry import load_registry
                        reg = load_registry(common.STATE_DIR)
                        shade_model = reg.get('shade_model') or '(same as main)'
                        lines.append(f'Shade model: {shade_model}')
                    except Exception as exc:
                        _diag('commands_mixin', 'model registry unreadable; shade model omitted from /models output', error=exc)

                    if lines:
                        common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                    else:
                        common.emit({'type': 'status', 'message': 'No local model servers detected.', 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/settings' or command == '/config':
                try:
                    lines = ['# Charon Settings', '']

                    # Provider
                    onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                    provider = str(onboarding.get('provider') or 'none')
                    model = str(onboarding.get('model') or onboarding.get('provider_model') or 'none')
                    project = str(onboarding.get('project') or 'none')
                    lines.append(f'Provider: {provider}')
                    lines.append(f'Model: {model}')
                    lines.append(f'Project: {project}')
                    lines.append(f'Setup complete: {onboarding.get("complete", False)}')
                    lines.append('')

                    # Shade model
                    try:
                        from charon.providers.model_registry import load_registry
                        reg = load_registry(common.STATE_DIR)
                        shade_mode = reg.get('shade_model_mode', 'auto')
                        shade_model = reg.get('shade_model') or '(same as main)'
                        shade_provider = reg.get('shade_provider') or '(same as main)'
                        shade_url = reg.get('shade_base_url') or '(default)'
                        lines.append(f'Shade model mode: {shade_mode}')
                        lines.append(f'Shade model: {shade_model}')
                        lines.append(f'Shade provider: {shade_provider}')
                        lines.append(f'Shade URL: {shade_url}')
                        lines.append('Shade model is also used for lightweight orchestration/NL parsing fallback.')
                    except Exception as exc:
                        _diag('commands_mixin', 'model registry unreadable; /settings shows shade model as not configured', error=exc)
                        lines.append('Shade model: (not configured)')
                    lines.append('')

                    # Autonomous mode
                    try:
                        from charon.agents.autonomous import load_autonomous_config
                        auto = load_autonomous_config(common.STATE_DIR)
                        lines.append(f'Autonomous mode: {"ON" if auto.get("enabled") else "OFF"}')
                        tb = auto.get('time_budget_minutes')
                        lines.append(f'Time budget: {tb} min' if tb else 'Time budget: unlimited')
                        lines.append(f'Git checkpoints: {"on" if auto.get("git_checkpoint") else "off"}')
                    except Exception as exc:
                        _diag('commands_mixin', 'autonomous config unreadable; /settings assumes autonomous mode OFF', error=exc)
                        lines.append('Autonomous mode: OFF')
                    lines.append('')

                    # Consolidation
                    try:
                        from charon.memory.consolidation import load_config
                        con = load_config(common.STATE_DIR)
                        lines.append(f'Consolidation: {"on" if con.get("enabled") else "off"}')
                        lines.append(f'Consolidation model: {con.get("model_tier", "fast")}')
                        lines.append(f'Consolidation interval: {con.get("scan_interval_heartbeats", 50)} heartbeats')
                    except Exception as exc:
                        _diag('commands_mixin', 'consolidation config unreadable; /settings shows defaults', error=exc)
                        lines.append('Consolidation: on (default)')
                    lines.append('')

                    # Approval
                    try:
                        from charon.infra.tool_approval import get_approval_status
                        status = get_approval_status(self._active_agent_id or 'default')
                        skip = status.get('skip_all', False)
                        approved = status.get('session_approved', [])
                        lines.append(f'Approval: {"DISABLED" if skip else "enabled"}')
                        if approved:
                            lines.append(f'Session approved: {", ".join(approved[:5])}')
                    except Exception as exc:
                        _diag('commands_mixin', 'approval status unreadable; /settings assumes approval enabled', error=exc)
                        lines.append('Approval: enabled')
                    lines.append('')

                    # Agent
                    try:
                        from charon.agents.agent_lifecycle import list_agents
                        agents = [a for a in list_agents() if a.get('role') == 'charon']
                        lines.append(f'Agents: {len(agents)}')
                        for a in agents[:5]:
                            lines.append(f'  {a.get("name", a.get("id", "?"))} ({a.get("id")}) — {a.get("status", "?")}')
                    except Exception as exc:
                        _diag('commands_mixin', 'agent listing failed; /settings omits agents section', error=exc)
                    lines.append('')

                    # Tools
                    from charon.tools import ALL_TOOL_DEFS
                    built_in = [t['name'] for t in ALL_TOOL_DEFS]
                    lines.append(f'Tools ({len(built_in)}): {", ".join(built_in)}')
                    try:
                        from charon.tools.dynamic_loader import list_dynamic_tools
                        dynamic = list_dynamic_tools()
                        if dynamic:
                            lines.append(f'Dynamic tools ({len(dynamic)}): {", ".join(t["name"] for t in dynamic)}')
                    except Exception as exc:
                        _diag('commands_mixin', 'dynamic tool listing failed; /settings omits dynamic tools', error=exc)

                    common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/approve' or command.startswith('/approve '):
                try:
                    from charon.infra.tool_approval import approve_tool_for_session, approve_for_session, get_approval_status
                    arg = command[9:].strip() if command.startswith('/approve ') else ''
                    # Approve for all possible session IDs to avoid mismatch
                    session_ids = set()
                    session_ids.add(self._active_agent_id or 'default')
                    session_ids.add('default')
                    # Also add the actual agent ID from the engine
                    if self.engine and self.engine.agent_id:
                        session_ids.add(self.engine.agent_id)
                    session_id = self._active_agent_id or 'default'

                    if arg == 'status':
                        status = get_approval_status(session_id)
                        skip = '(ALL CHECKS DISABLED)' if status['skip_all'] else ''
                        lines = [f'Approval status {skip}']
                        if status['session_approved']:
                            lines.append('Session approved:')
                            for a in status['session_approved']:
                                lines.append(f'  ✓ {a}')
                        if status['permanent_approved']:
                            lines.append('Permanently approved:')
                            for a in status['permanent_approved']:
                                lines.append(f'  ✓ {a}')
                        if not status['session_approved'] and not status['permanent_approved']:
                            lines.append('  (no approvals granted)')
                        common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                    elif not arg or arg == 'all':
                        # Approve all tools for all session IDs
                        for sid in session_ids:
                            for tool in ('Web', 'Http', 'Write', 'Edit', 'Bash', 'Git', 'SpawnBatch', 'SpawnShade', 'Browser', 'X'):
                                approve_tool_for_session(sid, tool)
                        common.emit({'type': 'status', 'message': '✓ All tools approved for this session.', 'request_id': request_id})
                    elif arg.startswith('network') or arg.startswith('web') or arg.startswith('http'):
                        for sid in session_ids:
                            approve_tool_for_session(sid, 'Web')
                            approve_tool_for_session(sid, 'Http')
                            approve_tool_for_session(sid, 'Browser')
                            approve_tool_for_session(sid, 'X')
                        common.emit({'type': 'status', 'message': '✓ Network tools approved for this session.', 'request_id': request_id})
                    elif arg.startswith('write') or arg.startswith('edit') or arg.startswith('file'):
                        for sid in session_ids:
                            approve_tool_for_session(sid, 'Write')
                            approve_tool_for_session(sid, 'Edit')
                            approve_tool_for_session(sid, 'Git')
                        common.emit({'type': 'status', 'message': '✓ File modification tools approved for this session.', 'request_id': request_id})
                    else:
                        for sid in session_ids:
                            approve_for_session(sid, arg)
                        common.emit({'type': 'status', 'message': f'✓ Approved: {arg}', 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command.startswith('/provider ') or command == '/provider':
                if command == '/provider':
                    onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                    provider = str(onboarding.get('provider') or 'none')
                    model = str(onboarding.get('model') or onboarding.get('provider_model') or 'none')
                    common.emit({'type': 'status', 'message': f'Current provider: {provider}/{model}', 'request_id': request_id})
                else:
                    new_provider = command[10:].strip()
                    if not new_provider:
                        common.emit({'type': 'error', 'error': 'Usage: /provider <name>', 'request_id': request_id})
                        return

                    current_provider = self._current_provider_name()
                    if new_provider == current_provider:
                        common.emit({'type': 'status', 'message': f'Already on {new_provider}.', 'request_id': request_id})
                        return

                    if self._has_transferable_context():
                        self._prompt_provider_switch(new_provider, request_id, source='provider')
                    else:
                        self._switch_provider_fresh(new_provider, request_id)
                return
            if command == '/shades' or command == '/shade stats':
                try:
                    from charon.shade.shade_stats import get_shade_stats, format_stats
                    stats = get_shade_stats(common.STATE_DIR)
                    if stats:
                        common.emit({'type': 'status', 'message': format_stats(stats), 'request_id': request_id})
                    else:
                        common.emit({'type': 'status', 'message': 'No shade usage recorded yet.', 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/specialist' or command.startswith('/specialist '):
                try:
                    from charon.agents import specialists as specialists_mod
                    from charon.agents import agent_lifecycle as lifecycle_mod
                    rest = command[len('/specialist'):].strip()
                    parts = rest.split(None, 2)
                    if not parts or parts[0] == 'list':
                        lines = ['Specialist templates:']
                        for key, label in specialists_mod.list_templates().items():
                            lines.append(f'  {key}: {label}')
                        active = [a for a in lifecycle_mod.list_agents()
                                  if a.get('specialization_locked')]
                        if active:
                            lines.append('Active specialists:')
                            for a in active:
                                lines.append(f"  {a.get('id')} {a.get('name', '')} "
                                             f"[{a.get('specialization', '')}] {a.get('project', '')}")
                        lines.append('Usage: /specialist create <template> [name] | '
                                     '/specialist assign <agent_id> <specialization>')
                        common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                    elif parts[0] == 'create' and len(parts) >= 2:
                        a = specialists_mod.create_specialist(
                            parts[1], name=parts[2] if len(parts) > 2 else None)
                        common.emit({'type': 'status',
                              'message': f"Created specialist {a['id']} ({a['name']}) — "
                                         f"{a.get('specialization', '')}",
                              'request_id': request_id})
                    elif parts[0] == 'assign' and len(parts) >= 3:
                        a = lifecycle_mod.assign_specialization(parts[1], parts[2])
                        if a:
                            common.emit({'type': 'status',
                                  'message': f"{a['id']} is now: {a.get('specialization', '')}",
                                  'request_id': request_id})
                        else:
                            common.emit({'type': 'error', 'error': f'Agent not found: {parts[1]}',
                                  'request_id': request_id})
                    else:
                        common.emit({'type': 'status',
                              'message': 'Usage: /specialist [list] | create <template> [name] | '
                                         'assign <agent_id> <specialization>',
                              'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/batch' or command.startswith('/batch '):
                batch_id = command[7:].strip() if command.startswith('/batch ') else ''
                try:
                    from charon.automation.batch_orchestrator import get_batch, list_batches, summarize_batch
                    if batch_id:
                        batch = get_batch(common.STATE_DIR, batch_id)
                        if batch:
                            lines = [summarize_batch(batch)]
                            for t in batch.get('tasks', []):
                                icon = {'completed': '✓', 'failed': '✗', 'in_progress': '◆', 'pending': '○'}.get(t.get('status', ''), '·')
                                model = t.get('model_used') or ''
                                complexity = t.get('complexity', 'normal')
                                model_tag = f' [{model}]' if model else ''
                                cx_tag = f' ({complexity})' if complexity != 'normal' else ''
                                summary = t.get('result_summary') or t.get('error') or ''
                                lines.append(f'  {icon} {t.get("title", "")}{cx_tag}{model_tag}: {summary[:50]}')
                            common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                        else:
                            common.emit({'type': 'error', 'error': f'Batch not found: {batch_id}', 'request_id': request_id})
                    else:
                        batches = list_batches(common.STATE_DIR)
                        if batches:
                            lines = [f'{len(batches)} batch(es):']
                            for b in batches[-10:]:
                                lines.append(f'  {summarize_batch(b)}')
                            common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                        else:
                            common.emit({'type': 'status', 'message': 'No batches.', 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/tools' or command == '/tools list':
                from charon.tools import ALL_TOOL_DEFS
                lines = ['Built-in tools:']
                for t in ALL_TOOL_DEFS:
                    lines.append(f'  {t["name"]}: {t["description"][:60]}')
                try:
                    from charon.tools.dynamic_loader import list_dynamic_tools, get_load_errors
                    dynamic = list_dynamic_tools()
                    if dynamic:
                        lines.append('\nDynamic tools:')
                        for t in dynamic:
                            lines.append(f'  {t["name"]}: {t["description"]}')
                            lines.append(f'    source: {t["source"]}')
                    errors = get_load_errors()
                    if errors:
                        lines.append('\nLoad errors:')
                        for e in errors:
                            lines.append(f'  {e["path"]}: {e["error"]}')
                except Exception as exc:
                    _diag('commands_mixin', 'dynamic tool listing failed; /tools shows built-ins only', error=exc)
                common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                return
            if command == '/tools reload':
                try:
                    from charon.tools.dynamic_loader import load_dynamic_tools
                    onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(common.ROOT)).strip()
                    defs, executors, errors = load_dynamic_tools(common.STATE_DIR, Path(project))
                    msg = f'Reloaded: {len(defs)} dynamic tool(s)'
                    if errors:
                        msg += f', {len(errors)} error(s)'
                        for e in errors:
                            msg += f'\n  {e["error"]}'
                    # Refresh engine tools
                    if self.engine:
                        from charon.tools.dynamic_loader import get_all_tool_defs
                        self.engine.tools = get_all_tool_defs(common.STATE_DIR, Path(project))
                    common.emit({'type': 'status', 'message': msg, 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': f'Reload failed: {e}', 'request_id': request_id})
                return
            if command == '/autonomous' or command == '/autonomous status':
                try:
                    from charon.agents.autonomous import load_autonomous_config, get_proposed_goals, get_goals_by_status
                    config = load_autonomous_config(common.STATE_DIR)
                    onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(common.ROOT)).strip()
                    proposed = get_proposed_goals(common.STATE_DIR, project=project)
                    confirmed = get_goals_by_status(common.STATE_DIR, project=project, status='confirmed')
                    executing = get_goals_by_status(common.STATE_DIR, project=project, status='executing')
                    msg = (
                        f'Autonomous mode: {"ON" if config.get("enabled") else "OFF"}\n'
                        f'Time budget: {config.get("time_budget_minutes") or "unlimited"} min\n'
                        f'Token budget: {config.get("token_budget") or "unlimited"}\n'
                        f'Git checkpoints: {"on" if config.get("git_checkpoint") else "off"}\n'
                        f'Goals — proposed: {len(proposed)}, confirmed: {len(confirmed)}, executing: {len(executing)}'
                    )
                    common.emit({'type': 'status', 'message': msg, 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/autonomous on':
                from charon.agents.autonomous import load_autonomous_config, save_autonomous_config
                config = load_autonomous_config(common.STATE_DIR)
                config['enabled'] = True
                save_autonomous_config(common.STATE_DIR, config)
                common.emit({'type': 'status', 'message': 'Autonomous mode ON. Agent will self-assign from confirmed goals.', 'request_id': request_id})
                return
            if command == '/autonomous off':
                from charon.agents.autonomous import load_autonomous_config, save_autonomous_config
                config = load_autonomous_config(common.STATE_DIR)
                config['enabled'] = False
                save_autonomous_config(common.STATE_DIR, config)
                common.emit({'type': 'status', 'message': 'Autonomous mode OFF.', 'request_id': request_id})
                return
            if command.startswith('/autonomous time '):
                try:
                    minutes = int(command[16:].strip())
                    from charon.agents.autonomous import load_autonomous_config, save_autonomous_config
                    config = load_autonomous_config(common.STATE_DIR)
                    config['time_budget_minutes'] = minutes
                    save_autonomous_config(common.STATE_DIR, config)
                    common.emit({'type': 'status', 'message': f'Time budget set to {minutes} minutes.', 'request_id': request_id})
                except ValueError:
                    common.emit({'type': 'error', 'error': 'Usage: /autonomous time <minutes>', 'request_id': request_id})
                return
            if command.startswith('/autonomous tokens '):
                try:
                    tokens = int(command[18:].strip())
                    from charon.agents.autonomous import load_autonomous_config, save_autonomous_config
                    config = load_autonomous_config(common.STATE_DIR)
                    config['token_budget'] = tokens
                    save_autonomous_config(common.STATE_DIR, config)
                    common.emit({'type': 'status', 'message': f'Token budget set to {tokens}.', 'request_id': request_id})
                except ValueError:
                    common.emit({'type': 'error', 'error': 'Usage: /autonomous tokens <count>', 'request_id': request_id})
                return
            if command == '/confirm' or command.startswith('/confirm '):
                goal_id = command[9:].strip() if command.startswith('/confirm ') else ''
                try:
                    from charon.agents.autonomous import confirm_goal, get_proposed_goals
                    onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(common.ROOT)).strip()
                    if not goal_id:
                        proposed = get_proposed_goals(common.STATE_DIR, project=project)
                        if proposed:
                            goal_id = proposed[0].get('goal_id', '')
                        else:
                            common.emit({'type': 'status', 'message': 'No proposed goals to confirm.', 'request_id': request_id})
                            return
                    result = confirm_goal(common.STATE_DIR, project=project, goal_id=goal_id)
                    if result:
                        common.emit({'type': 'status', 'message': f'Goal confirmed: {result.get("title", "")[:80]}', 'request_id': request_id})
                        common.emit({'type': 'refresh', 'payload': {'session_info': self._get_session_info()}, 'request_id': request_id})
                    else:
                        common.emit({'type': 'error', 'error': f'Goal not found: {goal_id}', 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/reject' or command.startswith('/reject '):
                goal_id = command[8:].strip() if command.startswith('/reject ') else ''
                try:
                    from charon.agents.autonomous import reject_goal, get_proposed_goals
                    onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(common.ROOT)).strip()
                    if not goal_id:
                        proposed = get_proposed_goals(common.STATE_DIR, project=project)
                        if proposed:
                            goal_id = proposed[0].get('goal_id', '')
                    if goal_id:
                        reject_goal(common.STATE_DIR, project=project, goal_id=goal_id)
                        common.emit({'type': 'status', 'message': 'Goal rejected (moved to backlog).', 'request_id': request_id})
                        common.emit({'type': 'refresh', 'payload': {'session_info': self._get_session_info()}, 'request_id': request_id})
                    else:
                        common.emit({'type': 'status', 'message': 'No proposed goals to reject.', 'request_id': request_id})
                except Exception as e:
                    common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/history' or command.startswith('/history '):
                agent_id = command[9:].strip() if command.startswith('/history ') else ''
                self.handle_agent_ledger(agent_id, request_id)
                return
            if command == '/consolidation' or command == '/consolidation status':
                self.handle_consolidation_traces(request_id)
                return
            if command == '/consolidation run':
                self.handle_consolidation_run(request_id)
                return
            if command.startswith('/consolidation model '):
                model_name = command[20:].strip()
                self.handle_consolidation_config({'action': 'set', 'config': {'model_tier': model_name}}, request_id)
                return
            if command.startswith('/consolidation interval '):
                try:
                    interval = int(command[23:].strip())
                    self.handle_consolidation_config({'action': 'set', 'config': {'scan_interval_heartbeats': interval}}, request_id)
                except ValueError:
                    common.emit({'type': 'error', 'error': 'Interval must be a number.', 'request_id': request_id})
                return
            if command == '/consolidation off':
                self.handle_consolidation_config({'action': 'set', 'config': {'enabled': False}}, request_id)
                return
            if command == '/consolidation on':
                self.handle_consolidation_config({'action': 'set', 'config': {'enabled': True}}, request_id)
                return
            if command == '/reset':
                if self.engine:
                    self.engine.reset()
                self.chat_history = []
                common.emit({'type': 'status', 'message': 'Conversation cleared.', 'request_id': request_id})
                return
            if command == '/model':
                onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                model = str(onboarding.get('model') or onboarding.get('provider_model') or 'none')
                provider = str(onboarding.get('provider') or 'none')
                # Show current model and trigger picker
                common.emit({'type': 'status', 'message': f'Current: {provider}/{model}', 'request_id': request_id})
                self._run_setup_command('model', request_id)
                return
            if command.startswith('/model '):
                model_name = command[7:].strip()
                if model_name:
                    self._run_setup_command(f'model {model_name}', request_id)
                return
            if command == '/provider':
                onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                provider = str(onboarding.get('provider') or 'none')
                common.emit({'type': 'status', 'message': f'Current provider: {provider}', 'request_id': request_id})
                # Show provider picker as menu
                common.emit({
                    'type': 'model_picker',
                    'models': [
                        {'id': 'claude-code', 'desc': 'Anthropic Claude (OAuth)'},
                        {'id': 'codex', 'desc': 'OpenAI Codex (OAuth)'},
                        {'id': 'lmstudio', 'desc': 'Local LM Studio'},
                        {'id': 'api', 'desc': 'Custom API endpoint'},
                    ],
                    'provider': 'switch',
                    'request_id': request_id,
                })
                return
            if command.startswith('/provider '):
                provider_name = command[10:].strip()
                if provider_name:
                    self._run_setup_command(f'provider {provider_name}', request_id)
                return

            # /fleet — remote agent team management
            if command == '/fleet' or command.startswith('/fleet '):
                rest = command[7:].strip() if command.startswith('/fleet ') else ''

                if rest == 'status' or not rest:
                    try:
                        from charon.fleet.fleet_registry import load_fleet
                        from charon.fleet.fleet_sync import get_cached_fleet_status
                        fleet = load_fleet()
                        cache = get_cached_fleet_status()
                        servers = fleet.get('servers', [])
                        if not servers:
                            common.emit({'type': 'status', 'message': 'No servers configured. Use /fleet setup <user@host> to add one.', 'request_id': request_id})
                        else:
                            for s in servers:
                                sid = s.get('id', s.get('host', '?'))
                                cached = cache.get(sid, {})
                                online = cached.get('online', False)
                                status_icon = '●' if online else '○'
                                common.emit({'type': 'status', 'message': f'  {status_icon} {sid} ({s.get("user", "")}@{s.get("host", "")})', 'request_id': request_id})
                                for a in s.get('agents', []):
                                    sessions = cached.get('sessions', {})
                                    agent_status = sessions.get(a['name'], {}).get('status', 'unknown')
                                    common.emit({'type': 'status', 'message': f'      {a["name"]} [{a.get("specialization", "")}] — {agent_status}', 'request_id': request_id})
                    except Exception as e:
                        common.emit({'type': 'error', 'error': f'Fleet status failed: {e}', 'request_id': request_id})
                    return

                if rest == 'setup' or rest.startswith('setup '):
                    target = rest[6:].strip() if rest.startswith('setup ') else ''
                    if not target:
                        common.emit({'type': 'status', 'message': 'Enter the server address (user@host):', 'request_id': request_id})
                        self._pending_fleet_setup = {'step': 'enter_host', 'request_id': request_id}
                        return
                    # Parse user@host
                    if '@' in target:
                        user, host = target.rsplit('@', 1)
                    else:
                        user, host = '', target
                    self._start_fleet_setup(host, user, request_id)
                    return

                common.emit({'type': 'error', 'error': 'Usage: /fleet setup <user@host> or /fleet status', 'request_id': request_id})
                return

            # /voyage — Harbor protocol: dispatch tasks to remote agent workers
            if command == '/voyage' or command.startswith('/voyage '):
                rest = command[8:].strip() if command.startswith('/voyage ') else ''

                if rest == 'list' or not rest:
                    from charon.fleet.harbor import list_voyages
                    voyages = list_voyages(common.STATE_DIR)
                    if not voyages:
                        common.emit({'type': 'status', 'message': 'No voyages. Use /voyage dispatch <server> <agent> <instruction>', 'request_id': request_id})
                    else:
                        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
                        common.emit({'type': 'status', 'message': '═══ Voyages ═══', 'request_id': request_id})
                        for v in voyages:
                            status = v.get('status', '?')
                            marker = '[~]' if status in ('started', 'running', 'dispatching') else '[x]' if status == 'completed' else '[!]' if status == 'failed' else '[ ]'
                            line = f'  {marker} {v["voyage_id"]}  {v["server"]}:{v["agent"]}  {v["instruction"][:50]}'
                            common.emit({'type': 'status', 'message': line, 'request_id': request_id})
                    return

                if rest.startswith('status '):
                    vid = rest[7:].strip()
                    from charon.fleet.harbor import get_voyage_status
                    v = get_voyage_status(vid, common.STATE_DIR)
                    if not v:
                        common.emit({'type': 'error', 'error': f'Voyage not found: {vid}', 'request_id': request_id})
                    else:
                        common.emit({'type': 'status', 'message': f'Voyage: {v.get("voyage_id")}', 'request_id': request_id})
                        common.emit({'type': 'status', 'message': f'Status: {v.get("status")}', 'request_id': request_id})
                        common.emit({'type': 'status', 'message': f'Server: {v.get("manifest", {}).get("server_id", "?")}:{v.get("manifest", {}).get("agent_name", "?")}', 'request_id': request_id})
                        common.emit({'type': 'status', 'message': f'Instruction: {v.get("manifest", {}).get("instruction", "")[:100]}', 'request_id': request_id})
                        for p in v.get('progress', [])[-5:]:
                            common.emit({'type': 'status', 'message': f'  {p.get("step", "")}: {p.get("summary", "")}', 'request_id': request_id})
                        result = v.get('result', {})
                        if result:
                            common.emit({'type': 'status', 'message': f'Return code: {result.get("returncode", "?")}', 'request_id': request_id})
                            stdout = result.get('stdout', '').strip()
                            if stdout:
                                for line in stdout.split('\n')[:20]:
                                    common.emit({'type': 'status', 'message': f'  {line}', 'request_id': request_id})
                    return

                if rest.startswith('dispatch '):
                    parts = rest[9:].strip().split(None, 2)
                    if len(parts) < 3:
                        common.emit({'type': 'error', 'error': 'Usage: /voyage dispatch <server_id> <agent_name> <instruction>', 'request_id': request_id})
                        return
                    server_id, agent_name, instruction = parts[0], parts[1], parts[2]

                    import threading
                    def _run_dispatch():
                        from charon.fleet.harbor import dispatch_voyage
                        vid = dispatch_voyage(
                            instruction=instruction,
                            server_id=server_id,
                            agent_name=agent_name,
                            project_root=common.ROOT,
                            state_dir=common.STATE_DIR,
                            on_status=lambda msg: common.emit({'type': 'status', 'message': msg, 'request_id': request_id}),
                        )
                        if not vid:
                            common.emit({'type': 'error', 'error': 'Dispatch failed', 'request_id': request_id})

                    threading.Thread(target=_run_dispatch, daemon=True).start()
                    return

                common.emit({'type': 'error', 'error': 'Usage: /voyage dispatch|status|list', 'request_id': request_id})
                return

            # /harvest_souls — scan sibling agent repos and interactively adopt abilities
            if command == '/harvest_souls' or command.startswith('/harvest_souls '):
                rest = command[len('/harvest_souls '):].strip() if command.startswith('/harvest_souls ') else ''

                if rest == 'status':
                    from charon.memory.assimilation import load_last_scan
                    scan = load_last_scan(common.STATE_DIR)
                    if not scan:
                        common.emit({'type': 'status', 'message': 'No scan found. Run /harvest_souls to scan agent repos.', 'request_id': request_id})
                    else:
                        common.emit({'type': 'status', 'message': f'Last scan: {scan.get("timestamp", "?")}', 'request_id': request_id})
                        common.emit({'type': 'status', 'message': f'Repos: {", ".join(scan.get("repos_scanned", []))} | Unavailable: {", ".join(scan.get("repos_unavailable", []))}', 'request_id': request_id})
                        common.emit({'type': 'status', 'message': f'Tools: {scan.get("total_tools", 0)} | Skills: {scan.get("total_skills", 0)} | Commands: {scan.get("total_commands", 0)}', 'request_id': request_id})
                        common.emit({'type': 'status', 'message': f'New abilities: {scan.get("new_abilities", 0)}', 'request_id': request_id})
                        adopted_file = common.STATE_DIR / 'assimilation' / 'adopted.json'
                        if adopted_file.exists():
                            try:
                                adopted = json.loads(adopted_file.read_text())
                                common.emit({'type': 'status', 'message': f'Adopted: {len(adopted)} abilities', 'request_id': request_id})
                            except Exception as exc:
                                _diag('commands_mixin', 'adopted.json unreadable; harvest status omits adopted count', error=exc)
                    return

                # /harvest_souls list — show numbered findings from last scan
                if rest == 'list':
                    self._harvest_souls_show_findings(request_id)
                    return

                # /harvest_souls evaluate — run capability-level gap evaluation from last scan
                if rest == 'evaluate':
                    import threading
                    def _run_gap_eval():
                        try:
                            from charon.memory.assimilation import run_gap_evaluation_from_saved_scan
                            clusters = run_gap_evaluation_from_saved_scan(
                                state_dir=common.STATE_DIR,
                                docs_dir=common.ROOT / 'docs',
                                charon_root=common.ROOT,
                                on_status=lambda msg: common.emit({'type': 'status', 'message': msg, 'request_id': request_id}),
                            )
                            if not clusters:
                                common.emit({'type': 'status', 'message': 'No saved scan found. Run /harvest_souls first.', 'request_id': request_id})
                            else:
                                common.emit({'type': 'status', 'message': f'Gap evaluation complete — {len(clusters)} capability clusters.', 'request_id': request_id})
                                self._harvest_souls_review(request_id)
                        except Exception as e:
                            common.emit({'type': 'error', 'error': f'Gap evaluation failed: {e}', 'request_id': request_id})
                    common.emit({'type': 'status', 'message': 'Evaluating capability gaps from last harvest scan...', 'request_id': request_id})
                    threading.Thread(target=_run_gap_eval, daemon=True).start()
                    return

                # /harvest_souls review — show capability-level harvest menu
                if rest == 'review':
                    self._harvest_souls_review(request_id)
                    return

                # /harvest_souls decide <N> — inspect capability-level decision
                if rest.startswith('decide '):
                    idx_str = rest[7:].strip()
                    self._harvest_souls_decide(idx_str, request_id)
                    return

                # /harvest_souls harvest <N|N,N,N|all> — queue capability clusters for assimilation
                if rest.startswith('harvest '):
                    selection = rest[8:].strip()
                    self._harvest_souls_harvest(selection, request_id)
                    return

                # /harvest_souls plan <N> — show implementation path for ability #N
                if rest.startswith('plan '):
                    idx_str = rest[5:].strip()
                    self._harvest_souls_plan(idx_str, request_id)
                    return

                # /harvest_souls adopt <N|N,N,N|all> — mark abilities for adoption
                if rest.startswith('adopt '):
                    selection = rest[6:].strip()
                    self._harvest_souls_adopt(selection, request_id)
                    return

                # /harvest_souls roadmap — show the full adoption roadmap
                if rest == 'roadmap':
                    self._harvest_souls_roadmap(request_id)
                    return

                # Default: run the scan, then show findings
                agent_filter = rest if rest and rest not in ('list', 'status', 'roadmap') else None

                import threading
                def _run_assimilation():
                    try:
                        from charon.memory.assimilation import run_full_assimilation
                        result = run_full_assimilation(
                            state_dir=common.STATE_DIR,
                            docs_dir=common.ROOT / 'docs',
                            charon_root=common.ROOT,
                            agent_filter=agent_filter,
                            on_status=lambda msg: common.emit({'type': 'status', 'message': msg, 'request_id': request_id}),
                        )
                        common.emit({'type': 'status', 'message': '', 'request_id': request_id})
                        common.emit({'type': 'status', 'message': f'Scan complete — {result.get("new_abilities", 0)} new abilities, {result.get("real_gaps", 0)} real capability gaps.', 'request_id': request_id})
                        # Show capability review inline when available; fall back to raw findings
                        self._harvest_souls_review(request_id)
                    except Exception as e:
                        common.emit({'type': 'error', 'error': f'Harvest failed: {e}', 'request_id': request_id})

                common.emit({'type': 'status', 'message': f'Scanning agent repos{f" ({agent_filter})" if agent_filter else ""}...', 'request_id': request_id})
                threading.Thread(target=_run_assimilation, daemon=True).start()
                return

            # Unknown command — show suggestions
            suggestions = self._get_suggestions(command)
            if suggestions:
                common.emit({
                    'type': 'suggestions',
                    'title': 'Did you mean?',
                    'items': suggestions,
                    'request_id': request_id,
                })
            else:
                common.emit({'type': 'error', 'error': f'Unknown command: {command}', 'request_id': request_id})
        except Exception as e:
            common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})
