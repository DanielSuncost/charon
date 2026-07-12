"""Work/automation slash-command handlers: clarify, idea, libris, devop,
monitor, automate, project, approve, batch, autonomous, confirm, reject.

Branch bodies are preserved verbatim from the original ``handle_command``
if/elif router in ``commands_mixin.py``; only the method wrappers and the
trailing ``return UNHANDLED`` are new. See ``CommandsMixin.handle_command``
for the dispatch.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from backend import common
from backend.commands_mixin import UNHANDLED
from backend.dashboard import _load_workflow_steps_spec
from backend.nlparse import _parse_interval_phrase
from backend.settings_io import _load_project_registry, _project_slug, _save_project_registry


class WorkCommandsMixin:
    """Handlers for the clarify/idea/libris/devop/automation command families."""

    def _cmd_clarifications(self, command: str, request_id: str | None):
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
        return UNHANDLED

    def _cmd_clarify(self, command: str, request_id: str | None):
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
        return UNHANDLED

    def _cmd_idea(self, command: str, request_id: str | None):
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
        return UNHANDLED

    def _cmd_ideas(self, command: str, request_id: str | None):
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
        return UNHANDLED

    def _cmd_idea_detail(self, command: str, request_id: str | None):
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
        return UNHANDLED

    def _cmd_libris(self, command: str, request_id: str | None):
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
        return UNHANDLED

    def _cmd_devop(self, command: str, request_id: str | None):
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
        return UNHANDLED

    def _cmd_monitor(self, command: str, request_id: str | None):
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
        return UNHANDLED

    def _cmd_automate(self, command: str, request_id: str | None):
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
        return UNHANDLED

    def _cmd_project(self, command: str, request_id: str | None):
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
        return UNHANDLED

    def _cmd_approve(self, command: str, request_id: str | None):
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
        return UNHANDLED

    def _cmd_batch(self, command: str, request_id: str | None):
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
        return UNHANDLED

    def _cmd_autonomous(self, command: str, request_id: str | None):
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
        return UNHANDLED

    def _cmd_confirm(self, command: str, request_id: str | None):
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
        return UNHANDLED

    def _cmd_reject(self, command: str, request_id: str | None):
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
        return UNHANDLED
