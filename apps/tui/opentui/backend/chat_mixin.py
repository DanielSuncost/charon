"""Chat streaming/worker mixin."""
from __future__ import annotations

import asyncio
import re
import threading
import time

from backend import common
from backend.nlparse import _natural_language_to_cron
from backend.settings_io import _full_messages_from_store


class ChatMixin:
    """Chat turn handling: streaming worker, abort/steer, conversation save."""

    def handle_chat(self, message: str, request_id: str | None):
        """Handle a chat message — run through conversation engine with streaming."""
        stripped = message.strip()
        if self._pending_fleet_setup:
            self._handle_fleet_setup_response(stripped, request_id)
            return
        if self._pending_provider_switch and stripped in {'1', '2'}:
            self.handle_command(f'/{stripped}', request_id)
            return
        if message.startswith('/'):
            self.handle_command(message, request_id)
            return

        # Natural-language Libris trigger
        libris_match = re.match(r'^(?:start|run|launch)\s+(?:a\s+)?libris\s+(?:research\s+project|research|project)?\s*(?:on|for)?\s+(.+)$', stripped, re.I)
        if libris_match:
            topic_prompt = libris_match.group(1).strip()
            self.handle_command(f'/libris {topic_prompt}', request_id)
            return

        # Natural-language software-dev trigger
        devop_match = re.match(
            r'^(?:start|run|launch|begin|kick\s+off|create)\s+(?:an?\s+)?(?:autonomous\s+)?(?:software\s+(?:development|dev)|software|dev|coding)\s+(?:project|operation|build)?\s*(?:that|to|for)?\s+(.+)$',
            stripped,
            re.I,
        )
        if devop_match:
            build_prompt = devop_match.group(1).strip()
            self.handle_command(f'/devop {build_prompt}', request_id)
            return

        cron_nl = _natural_language_to_cron(stripped)
        cron_url_match = re.search(r'check\s+(https?://\S+)', stripped, re.I)
        if cron_nl and cron_url_match:
            url = cron_url_match.group(1).rstrip('.,)')
            self.handle_command(f'/automate cron "{cron_nl}" check {url}', request_id)
            return

        monitor_match = re.match(r'^(?:every\s+.+?|hourly|daily)\s+check\s+(https?://\S+)(?:\s+and\s+report\s+if\s+it\s+(?:isn\'t|is\s+not|fails?|breaks?))?$', stripped, re.I)
        if monitor_match:
            interval_phrase = stripped[:monitor_match.start(1)].strip()
            url = monitor_match.group(1).rstrip('.,)')
            self.handle_command(f'/monitor {interval_phrase} {url}', request_id)
            return

        continuous_match = re.match(r'^(?:continuously|nonstop|always on|all day)\s+check\s+(https?://\S+)(?:.*)?$', stripped, re.I)
        if continuous_match:
            url = continuous_match.group(1).rstrip('.,)')
            self.handle_command(f'/automate continuous check {url}', request_id)
            return

        route = self._match_nl_orchestration_command(stripped)
        if route:
            command, status = route
            source = 'shades-parser' if 'via shades parser' in status.lower() else ('fast-path' if 'via fast-path' in status.lower() else 'unknown')
            self._last_orchestration_parse = {
                'source': source,
                'status': status,
                'command': command,
                'input': stripped[:240],
                'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            }
            common.emit({'type': 'status', 'message': status, 'request_id': request_id})
            common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
            self.handle_command(command, request_id)
            return

        engine, error = self._ensure_engine()
        if not engine:
            common.emit({'type': 'error', 'error': error, 'request_id': request_id})
            return
        # _ensure_engine may assign a fresh session id; persist any in-memory
        # outcome state now that we have one.
        self._save_session_outcomes()

        # Session outcome ledger: resolve the previous active task based on
        # how the user is steering now, then start a new active task if this
        # message is a concrete request.
        if self._session_tasks:
            if self._is_ack_message(message) or self._starts_with_ack(message):
                self._resolve_pending_outcome('completed')
            elif self._is_redirect_message(message):
                self._resolve_pending_outcome('failed')
            elif self._parse_intent(message):
                self._resolve_pending_outcome('completed')
        self._start_outcome_for_message(message)
        self._improve_active_outcome_title_background(message, request_id)
        common.emit({
            'type': 'refresh',
            'payload': {'session_info': self._get_session_info()},
            'request_id': request_id,
        })

        self.chat_history.append({'role': 'user', 'content': message})

        async def _run():
            text_parts = []
            thinking_started = False
            _tool_calls_record = []
            _total_input_tokens = 0
            _total_output_tokens = 0
            _total_turns = 0
            try:
                async for event in engine.submit(message):
                    if event.type == 'thinking_delta':
                        if not thinking_started:
                            common.emit({'type': 'thinking_start', 'request_id': request_id})
                            thinking_started = True
                        if self.visible_thoughts and self._thoughts_supported():
                            common.emit({
                                'type': 'thinking_delta',
                                'text': event.data.get('text', ''),
                                'request_id': request_id,
                            })
                    elif event.type == 'text_delta':
                        text = event.data.get('text', '')
                        text_parts.append(text)
                        common.emit({'type': 'chat_delta', 'text': text, 'request_id': request_id})
                    elif event.type == 'tool_call':
                        _tool_calls_record.append({
                            'tool': event.data.get('tool_name', ''),
                            'arguments': event.data.get('arguments', {}),
                        })
                        common.emit({
                            'type': 'tool_call',
                            'tool_name': event.data.get('tool_name', ''),
                            'arguments': event.data.get('arguments', {}),
                            'tool_call_id': event.data.get('tool_call_id', ''),
                            'request_id': request_id,
                        })
                    elif event.type == 'tool_execution_output':
                        common.emit({
                            'type': 'tool_result_delta',
                            'tool_name': event.data.get('tool_name', ''),
                            'content': event.data.get('content', ''),
                            'chunk': event.data.get('chunk', ''),
                            'tool_call_id': event.data.get('tool_call_id', ''),
                            'request_id': request_id,
                        })
                    elif event.type == 'tool_execution_end':
                        # Update the last tool call record with result
                        if _tool_calls_record:
                            _tool_calls_record[-1]['result'] = event.data.get('content', '')[:500]
                            _tool_calls_record[-1]['is_error'] = event.data.get('is_error', False)
                        common.emit({
                            'type': 'tool_result',
                            'tool_name': event.data.get('tool_name', ''),
                            'content': event.data.get('content', ''),
                            'is_error': event.data.get('is_error', False),
                            'truncated': event.data.get('truncated', False),
                            'tool_call_id': event.data.get('tool_call_id', ''),
                            'request_id': request_id,
                        })
                    elif event.type == 'turn_end':
                        _total_turns += 1
                    elif event.type == 'message_end':
                        usage = event.data.get('usage', {})
                        input_tokens = int(usage.get('input_tokens', 0) or 0)
                        output_tokens = int(usage.get('output_tokens', 0) or 0)

                        # Fallback for providers like LM Studio that may not emit usage
                        # in streamed responses: estimate from current context + response.
                        if self.engine and input_tokens <= 0:
                            try:
                                input_tokens = sum(
                                    len((getattr(m, 'content', '') or '')) // 4
                                    for m in self.engine.messages[:-1]
                                )
                            except Exception:
                                input_tokens = 0
                        if output_tokens <= 0:
                            output_tokens = len((event.data.get('content', '') or '')) // 4

                        _total_input_tokens += input_tokens
                        _total_output_tokens += output_tokens

                        # Context = input + output tokens from this request
                        # (input_tokens = entire context sent to the API)
                        context_tokens = input_tokens + output_tokens
                        context_pct = 0
                        context_window = 200000

                        if self.engine:
                            try:
                                context_window = int(getattr(self.engine.model, 'context_window', 200000) or 200000)
                            except Exception:
                                context_window = 200000

                        if context_tokens > 0:
                            context_pct = min(100, int(context_tokens * 100 / max(1, context_window)))
                        elif self.engine:
                            # Fallback: estimate from message content
                            msg_tokens = sum(
                                len(getattr(m, 'content', '') or '') // 4
                                for m in self.engine.messages
                            )
                            context_pct = min(100, int(msg_tokens * 100 / max(1, context_window)))
                            context_tokens = msg_tokens

                        common.emit({
                            'type': 'usage',
                            'input_tokens': input_tokens,
                            'output_tokens': output_tokens,
                            'context_tokens': context_tokens,
                            'context_pct': context_pct,
                            'context_window': context_window,
                            'request_id': request_id,
                        })
                    elif event.type == 'turn_end':
                        common.emit({
                            'type': 'turn_complete',
                            'stop_reason': event.data.get('stop_reason', ''),
                            'turn': event.data.get('turn', 0),
                            'request_id': request_id,
                        })
                    elif event.type == 'error':
                        common.emit({
                            'type': 'error',
                            'error': event.data.get('error', 'unknown error'),
                            'request_id': request_id,
                        })
                    elif event.type == 'steer_delivered':
                        common.emit({
                            'type': 'steer_delivered',
                            'content': event.data.get('content', ''),
                            'skipped_tools': event.data.get('skipped_tools', 0),
                            'request_id': request_id,
                        })
                    elif event.type == 'follow_up_delivered':
                        common.emit({
                            'type': 'follow_up_delivered',
                            'content': event.data.get('content', ''),
                            'request_id': request_id,
                        })
                    elif event.type == 'retry':
                        attempt = event.data.get('attempt', 1)
                        wait = event.data.get('wait_seconds', 3)
                        common.emit({'type': 'status', 'message': f'⟳ Retrying (attempt {attempt}/2, waiting {wait}s)...', 'request_id': request_id})
                    elif event.type == 'compaction_start':
                        common.emit({'type': 'status', 'message': 'Compacting context...', 'request_id': request_id})
                    elif event.type == 'compaction_end':
                        common.emit({'type': 'status', 'message': 'Context compacted.', 'request_id': request_id})

            except Exception as e:
                common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})

            full_text = ''.join(text_parts)
            self.chat_history.append({'role': 'assistant', 'content': full_text})

            # Record task in working memory + task queue (zero LLM cost)
            if self._active_agent_id and engine:
                try:
                    from charon.agents.task_summarizer import summarize_fast
                    from charon.agents.agent_runtime import update_working_memory
                    from charon.memory.execution_memory import create_task_episode
                    import uuid as _uuid

                    task_id = f'chat-{_uuid.uuid4().hex[:8]}'
                    # Get the user message that triggered this
                    user_msg = message[:200] if message else ''

                    summary = summarize_fast(
                        instruction=user_msg,
                        tool_calls=_tool_calls_record,
                        response_text=full_text,
                        errors=[],
                        total_turns=_total_turns,
                    )

                    # Only write to persistent agent memory when this session
                    # was explicitly bound to a persistent agent. Fresh sessions
                    # should not silently share working memory.
                    memory_agent_id = getattr(self, '_bound_agent_id', None)
                    if memory_agent_id:
                        update_working_memory(
                            common.STATE_DIR, memory_agent_id,
                            task_id=task_id, summary=summary,
                        )

                    # Update the session outcome ledger entry for this task.
                    if not hasattr(self, '_session_tasks'):
                        self._session_tasks = []
                    files_touched = []
                    try:
                        for tc in _tool_calls_record:
                            args = tc.get('arguments', {}) or {}
                            for key in ('path', 'oldText', 'newText'):
                                val = args.get(key)
                                if key == 'path' and isinstance(val, str) and val and val not in files_touched:
                                    files_touched.append(val)
                    except Exception:
                        files_touched = []

                    # Determine if the agent concluded the task or is mid-flight
                    # (e.g. asking a clarifying question). We consider a task done
                    # if it made at least one tool call (did real work) OR if the
                    # response text doesn't look like a question/clarification.
                    agent_concluded = (
                        len(_tool_calls_record) > 0
                        or not self._is_question_message(full_text.strip())
                    )
                    new_status = 'completed' if agent_concluded else 'active'

                    updated = False
                    for item in reversed(self._session_tasks):
                        if item.get('status') == 'active':
                            item['status'] = new_status
                            if new_status == 'completed':
                                item['resolved_at'] = time.time()
                            item['summary'] = summary
                            item['detail'] = (
                                f'Task: {user_msg[:100]}\n'
                                f'Outcome: {item.get("title", "")}\n'
                                f'Result: {summary}\n'
                                f'Tools: {len(_tool_calls_record)} calls, {_total_turns} turns\n'
                                f'Tokens: {_total_input_tokens}↑ {_total_output_tokens}↓'
                            )
                            item['tokens_in'] = _total_input_tokens
                            item['tokens_out'] = _total_output_tokens
                            item['tool_calls'] = len(_tool_calls_record)
                            item['turns'] = _total_turns
                            item['files_touched'] = files_touched
                            item['ts'] = time.time()
                            updated = True
                            break
                    if not updated and self._parse_intent(user_msg):
                        self._start_outcome_for_message(user_msg)
                        if self._session_tasks:
                            self._session_tasks[-1]['status'] = new_status
                            if new_status == 'completed':
                                self._session_tasks[-1]['resolved_at'] = time.time()
                            self._session_tasks[-1]['summary'] = summary
                            self._session_tasks[-1]['detail'] = f'Task: {user_msg[:100]}\nResult: {summary}'
                            self._session_tasks[-1]['tokens_in'] = _total_input_tokens
                            self._session_tasks[-1]['tokens_out'] = _total_output_tokens
                            self._session_tasks[-1]['tool_calls'] = len(_tool_calls_record)
                            self._session_tasks[-1]['turns'] = _total_turns
                            self._session_tasks[-1]['files_touched'] = files_touched
                    self._save_session_outcomes()
                    common.emit({
                        'type': 'refresh',
                        'payload': {'session_info': self._get_session_info()},
                        'request_id': request_id,
                    })

                    try:
                        create_task_episode(
                            common.STATE_DIR,
                            session_id=self._active_agent_id,
                            agent_id=memory_agent_id or '',
                            project_root=str(engine.project_root),
                            provider=str(self._current_provider_name() or getattr(engine, 'provider_name', 'unknown')),
                            objective=user_msg,
                            summary=summary,
                            tool_calls=_tool_calls_record,
                            response_text=full_text,
                            total_turns=_total_turns,
                            input_tokens=_total_input_tokens,
                            output_tokens=_total_output_tokens,
                        )
                    except Exception:
                        pass
                except Exception:
                    pass

            # Persist conversation
            if self._active_agent_id and engine:
                try:
                    # When lossless store is active, messages are already persisted
                    # to SQLite on every turn.  Write JSONL as backup using the
                    # FULL history from the store (not engine.messages which may
                    # have been truncated by legacy compaction).
                    from charon.conversation.conversation_store import save_conversation, message_to_dict
                    msgs_to_save = None
                    if engine.has_lossless_store:
                        msgs_to_save = _full_messages_from_store(self._active_agent_id)
                    if msgs_to_save is None:
                        msgs_to_save = list(engine.messages)
                    save_conversation(common.STATE_DIR, self._active_agent_id,
                        [message_to_dict(m) for m in msgs_to_save])
                    # Register session on first save (not on startup)
                    if not hasattr(self, '_session_registered'):
                        self._session_registered = True
                        try:
                            from charon.agents.session_registry import register_session
                            register_session(common.STATE_DIR, self._active_agent_id)
                        except Exception:
                            pass
                except Exception:
                    pass

            common.emit({
                'type': 'chat_complete',
                'summary': full_text[:200],
                'request_id': request_id,
            })

            # Write to persistent agent inbox only for explicitly bound agents.
            try:
                bound_agent_id = getattr(self, '_bound_agent_id', None)
                if bound_agent_id:
                    from charon.infra.store_adapter import get_db
                    from charon.infra.store import agent_inbox_push
                    db = get_db(common.STATE_DIR)
                    agent_inbox_push(db, bound_agent_id,
                        event_type='task_received',
                        payload={'instruction': message[:200], 'summary': full_text[:200]})
            except Exception:
                pass

        asyncio.run(_run())

    def _chat_worker(self, message: str, request_id: str | None):
        """Run handle_chat on a worker thread."""
        try:
            with self._engine_lock:
                self.handle_chat(message, request_id)
        finally:
            self._chat_busy = False

    def _start_background_worker(self):
        """Start a daemon thread that runs periodic background tasks.

        Runs consolidation, goal inference, and emits heartbeat events
        even while the chat engine is busy processing a message.
        """
        def _bg_loop():
            import time as _time
            cycle = 0
            consolidation_interval = 50   # ~100 seconds
            goal_inference_interval = 30  # ~60 seconds
            last_consolidation = 0
            last_goal_inference = 0

            while True:
                _time.sleep(2)
                cycle += 1

                # Heartbeat event for the run log (so dashboard activity picks it up)
                if cycle % 30 == 0:
                    try:
                        from charon.infra.store_adapter import get_db
                        from charon.infra.store import run_log_append
                        db = get_db(common.STATE_DIR)
                        run_log_append(db, 'heartbeat', cycle=cycle,
                                       uptime_seconds=cycle * 2)
                    except Exception:
                        pass

                # Consolidation check
                if cycle - last_consolidation >= consolidation_interval:
                    last_consolidation = cycle
                    try:
                        from charon.memory.consolidation import load_config, should_run, run_consolidation
                        config = load_config(common.STATE_DIR)
                        if config.get('enabled', True) and should_run(common.STATE_DIR, config):
                            result = run_consolidation(common.STATE_DIR, config)
                            changes = result.get('changes', [])
                            if changes:
                                common.emit({
                                    'type': 'status',
                                    'message': f'🧠 User model updated: {len(changes)} change(s)',
                                })
                    except Exception:
                        pass

                # Goal inference — always runs when there are enough messages
                # (independent of autonomous mode, which controls self-assignment)
                if cycle - last_goal_inference >= goal_inference_interval:
                    last_goal_inference = cycle
                    try:
                        from charon.agents.autonomous import (
                            infer_goals_from_conversation,
                            propose_goal, get_proposed_goals,
                        )
                        if (self.engine and
                                len(self.engine.messages) >= 4 and
                                not self._chat_busy):
                            # Only infer if there are enough messages and we're not mid-chat
                            import asyncio as _aio
                            onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                            project = str(onboarding.get('project') or str(common.ROOT)).strip()

                            # Check if we already have proposed goals waiting
                            existing_proposed = get_proposed_goals(common.STATE_DIR, project=project)
                            if len(existing_proposed) < 3:  # don't spam proposals
                                from charon.providers.provider_bridge import create_provider_and_model
                                provider, model, ready = create_provider_and_model(common.STATE_DIR)
                                if ready:
                                    self._goal_inference_token_estimate += 1000
                                    goals = _aio.run(infer_goals_from_conversation(
                                        common.STATE_DIR,
                                        agent_id=self._active_agent_id or '',
                                        messages=self.engine.messages,
                                        provider=provider,
                                        model=model,
                                    ))
                                    for g in goals[:2]:  # max 2 proposals per cycle
                                        proposed = propose_goal(
                                            common.STATE_DIR,
                                            agent_id=self._active_agent_id or '',
                                            project=project,
                                            title=g.get('title', ''),
                                            acceptance_criteria=g.get('acceptance_criteria', []),
                                            plan=g.get('plan', []),
                                        )
                                        common.emit({
                                            'type': 'status',
                                            'message': (
                                                f'💡 Goal proposed: {proposed["title"][:80]}\n'
                                                f'   /confirm to approve, /reject to defer'
                                            ),
                                        })
                                        common.emit({'type': 'refresh', 'payload': {'session_info': self._get_session_info()}})
                    except Exception:
                        pass

                # Process queued shade phase + cron tasks (when not chatting)
                if cycle % 3 == 0 and not self._chat_busy:
                    try:
                        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                        import uuid as _uuid
                        from charon.conversation.conversation_runtime import load_queue, save_queue
                        queue = load_queue(common.STATE_DIR)
                        now_iso = _dt.now(_tz.utc).isoformat()

                        def _is_due(t: dict, *, now_iso=now_iso) -> bool:
                            nb = t.get('not_before')
                            return (not nb) or (str(nb) <= now_iso)

                        def _is_cron_task(t: dict) -> bool:
                            return str(t.get('correlation_id') or '').startswith('cron:')

                        pending = [
                            t for t in queue
                            if t.get('status') == 'pending'
                            and _is_due(t)
                            and (t.get('shade_phase') or _is_cron_task(t))
                        ]

                        if pending:
                            task = pending[0]
                            agent_id = task.get('owner_agent_id') or task.get('actor_agent_id', '')
                            if agent_id:
                                from charon.agents.agent_lifecycle import list_agents
                                agent = None
                                for a in list_agents():
                                    if a.get('id') == agent_id:
                                        agent = a
                                        break
                                if agent:
                                    task['status'] = 'in_progress'
                                    task['started_at'] = now_iso
                                    save_queue(common.STATE_DIR, queue)

                                    from charon.agents.agent_runtime import run_task_tick
                                    ok, result = run_task_tick(common.STATE_DIR, task, agent=agent)

                                    task['status'] = 'completed' if ok else 'failed'
                                    if ok:
                                        task['result_summary'] = (result or {}).get('summary', '')
                                    else:
                                        task['last_error'] = result
                                    task['completed_at'] = _dt.now(_tz.utc).isoformat()
                                    task['updated_at'] = task['completed_at']
                                    save_queue(common.STATE_DIR, queue)

                                    # TUI-side recurrence for cron tasks, mirroring charon_loop behavior.
                                    if ok and _is_cron_task(task):
                                        interval = task.get('interval_minutes')
                                        if interval and isinstance(interval, (int, float)) and interval > 0:
                                            next_run = _dt.now(_tz.utc) + _td(minutes=float(interval))
                                            recurring_copy = {
                                                'id': f"{str(task.get('id') or 'task').split('-')[0]}-{_uuid.uuid4().hex[:8]}",
                                                'title': task.get('title', ''),
                                                'instruction': task.get('instruction', ''),
                                                'status': 'pending',
                                                'task_type': task.get('task_type', 'agent_task'),
                                                'owner_agent_id': task.get('owner_agent_id'),
                                                'actor_agent_id': task.get('actor_agent_id'),
                                                'project': task.get('project'),
                                                'priority': task.get('priority', 'normal'),
                                                'created_at': _dt.now(_tz.utc).isoformat(),
                                                'updated_at': _dt.now(_tz.utc).isoformat(),
                                                'attempt_count': 0,
                                                'max_attempts': int(task.get('max_attempts') or 3),
                                                'interval_minutes': interval,
                                                'not_before': next_run.isoformat(),
                                                'correlation_id': task.get('correlation_id'),
                                                'constraints': task.get('constraints') or [],
                                                'expected_outputs': task.get('expected_outputs') or [],
                                            }
                                            queue.append(recurring_copy)
                                            save_queue(common.STATE_DIR, queue)
                    except Exception:
                        pass

                # Monitor batches and report completion
                if cycle % 5 == 0:  # check every 10 seconds
                    try:
                        from charon.automation.batch_orchestrator import list_batches, summarize_batch
                        # _notified_batches is pre-populated at startup

                        all_batches = list_batches(common.STATE_DIR)
                        has_running = False
                        for b in all_batches:
                            bid = b.get('id', '')
                            status = b.get('status', '')

                            if status == 'running':
                                has_running = True

                            # Notify on completion (only once per batch)
                            if status in ('completed', 'partial') and bid not in self._notified_batches:
                                self._notified_batches.add(bid)

                                # Build per-task results
                                lines = [f'⚡ Batch complete: {summarize_batch(b)}']
                                for t in b.get('tasks', []):
                                    icon = '✓' if t.get('status') == 'completed' else '✗'
                                    summary = (t.get('result_summary') or t.get('error') or '')[:60]
                                    lines.append(f'  {icon} {t.get("title", "")}: {summary}')

                                common.emit({'type': 'status', 'message': '\n'.join(lines)})

                        if not has_running and self.agent_mode == 'delegating':
                            self.agent_mode = 'interactive'
                    except Exception:
                        pass

                    # Update agent mode based on state
                    try:
                        from charon.automation.batch_orchestrator import list_batches
                        from charon.agents.autonomous import load_autonomous_config
                        running_batches = list_batches(common.STATE_DIR, status='running')
                        auto_cfg = load_autonomous_config(common.STATE_DIR)

                        if self._chat_busy:
                            # User is actively chatting — always interactive
                            # (batch progress still shows separately in status bar)
                            self.agent_mode = 'interactive'
                        elif running_batches:
                            self.agent_mode = 'delegating'
                        elif auto_cfg.get('enabled'):
                            self.agent_mode = 'autonomous'
                        else:
                            self.agent_mode = 'idle' if not self.engine else 'interactive'
                    except Exception:
                        pass

        t = threading.Thread(target=_bg_loop, daemon=True)
        t.start()

    def _save_conversation_now(self):
        """Save current conversation state immediately (called on exit)."""
        if self._active_agent_id and self.engine and self.engine.messages:
            try:
                from charon.conversation.conversation_store import save_conversation, message_to_dict
                # Use full history from lossless store when available
                msgs_to_save = None
                if self.engine.has_lossless_store:
                    msgs_to_save = _full_messages_from_store(self._active_agent_id)
                if msgs_to_save is None:
                    msgs_to_save = list(self.engine.messages)
                save_conversation(common.STATE_DIR, self._active_agent_id,
                    [message_to_dict(m) for m in msgs_to_save])
            except Exception:
                pass
        # Unregister live session
        if self._active_agent_id:
            try:
                from charon.agents.session_registry import unregister_session
                unregister_session(common.STATE_DIR, self._active_agent_id)
            except Exception:
                pass

    def handle_abort(self, request_id: str | None):
        stopped_tool = False
        try:
            from charon.tools import abort_running_bash
            stopped_tool = abort_running_bash()
        except Exception:
            stopped_tool = False
        if self.engine:
            self.engine.abort()
            msg = 'Aborted current run.'
            if stopped_tool:
                msg += ' Active bash command killed.'
            common.emit({'type': 'status', 'message': msg, 'request_id': request_id})
        elif stopped_tool:
            common.emit({'type': 'status', 'message': 'Killed active bash command.', 'request_id': request_id})

    def handle_steer(self, message: str, request_id: str | None):
        """Interrupt the agent mid-execution with a new instruction."""
        if self.engine:
            self.engine.steer(message)
            common.emit({'type': 'steer_queued', 'message': message,
                  'pending': self.engine.pending_messages,
                  'request_id': request_id})
        else:
            common.emit({'type': 'error', 'error': 'No active engine to steer.',
                  'request_id': request_id})

    def handle_follow_up(self, message: str, request_id: str | None):
        """Queue a message for after the agent finishes."""
        if self.engine:
            self.engine.follow_up(message)
            common.emit({'type': 'follow_up_queued', 'message': message,
                  'pending': self.engine.pending_messages,
                  'request_id': request_id})
        else:
            common.emit({'type': 'error', 'error': 'No active engine for follow-up.',
                  'request_id': request_id})
