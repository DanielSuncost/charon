"""Libris intake/launch mixin."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from backend import common


class LibrisMixin:
    """Libris research-run intake and launch helpers."""

    def _libris_project_root(self) -> str:
        onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
        configured_project = str(onboarding.get('project') or '').strip()
        return configured_project or str(common.ROOT)

    def _devop_project_root(self) -> str:
        onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
        configured_project = str(onboarding.get('project') or '').strip()
        return configured_project or str(common.ROOT)

    def _libris_goal_options(self, prompt: str) -> list[str]:
        lower = (prompt or '').lower()
        if 'computer vision' in lower or 'vision' in lower:
            return [
                'Identify the most practically important new techniques worth implementing or prototyping.',
                'Focus on the highest-novelty research directions from the last few months, even if speculative.',
                'Prioritize methods with strong evidence, benchmarks, code availability, and likely near-term impact.',
            ]
        if 'reinforcement learning' in lower or 'rl' in lower:
            return [
                'Prioritize techniques most likely to improve our current RL work in practice.',
                'Focus on the most novel and strategically important RL directions from recent months.',
                'Prefer methods with strong empirical evidence, code, and realistic implementation paths.',
            ]
        return [
            'Prioritize practical, high-impact techniques we could plausibly act on.',
            'Focus on novelty and strategic importance, even if implementation is less immediate.',
            'Prefer evidence-backed methods with code, benchmarks, and clear adoption signals.',
        ]

    def _libris_extract_stop(self, text: str) -> str:
        t = (text or '').strip()
        m = re.search(r'(stop after .+|run for .+|for \d+ (?:hours?|days?|weeks?)|until i stop you|until stopped|cap(?: it)? at .+ tokens?|under .+ tokens?)', t, re.I)
        return m.group(1).strip() if m else ''

    def _libris_parse_budget(self, stop_condition: str) -> dict:
        t = (stop_condition or '').lower().strip()
        out: dict = {}
        if not t:
            return out
        m = re.search(r'(\d+)\s*(hour|hours|day|days|week|weeks)', t)
        if m:
            n = int(m.group(1))
            unit = m.group(2)
            hours = n
            if 'day' in unit:
                hours = n * 24
            elif 'week' in unit:
                hours = n * 24 * 7
            out['max_wall_hours'] = hours
        m = re.search(r'(\d+(?:[\.,]\d+)?)\s*(m|million)?\s*tokens?', t)
        if m:
            num = float(m.group(1).replace(',', ''))
            if m.group(2) == 'm':
                num *= 1_000_000
            out['max_total_tokens'] = int(num)
        m = re.search(r'\$\s*(\d+(?:\.\d+)?)', t)
        if m:
            out['max_total_cost_usd'] = float(m.group(1))
        return out

    def _libris_has_clear_goal(self, text: str) -> bool:
        t = (text or '').lower()
        patterns = [
            r'priorit', r'focus on', r'looking for', r'goal is', r'what we care about',
            r'practical', r'novel', r'implementation', r'actionable', r'benchmark',
        ]
        return any(re.search(p, t) for p in patterns)

    def _load_libris_swarm(self, operation_id: str) -> dict[str, Any]:
        op_id = str(operation_id or '').strip()
        if not op_id:
            return {}
        from charon.libris.libris_runtime import get_libris_swarm_state
        return get_libris_swarm_state(
            common.STATE_DIR,
            Path(self._libris_project_root()),
            op_id,
        )

    def _latest_libris_operation_id(self) -> str:
        """Return the most recently updated operation for the active project."""
        from charon.libris.libris_runtime import operations_root

        root = operations_root(
            common.STATE_DIR,
            Path(self._libris_project_root()),
        )
        if not root.exists():
            return ''

        candidates: list[tuple[str, int, str]] = []
        for operation_path in root.iterdir():
            if not operation_path.is_dir():
                continue
            state_path = operation_path / 'operation.json'
            state = common._load_json(state_path, {})
            if not isinstance(state, dict) or not state:
                continue
            operation_id = str(
                state.get('operation_id') or operation_path.name
            ).strip()
            if not operation_id:
                continue
            updated_at = str(
                state.get('updated_at') or state.get('created_at') or ''
            ).strip()
            try:
                modified_ns = state_path.stat().st_mtime_ns
            except OSError:
                modified_ns = 0
            candidates.append((updated_at, modified_ns, operation_id))

        return max(candidates, default=('', 0, ''))[2]

    def _resolve_libris_operation_id(self, text: str = '') -> str:
        match = re.search(r'\brop_[A-Za-z0-9_-]+\b', str(text or ''))
        if match:
            return match.group(0)
        recent = str(
            getattr(self, '_last_libris_operation_id', '') or ''
        ).strip()
        if not recent:
            try:
                recent = self._latest_libris_operation_id()
            except Exception:
                recent = ''
            if recent:
                self._last_libris_operation_id = recent
        return recent

    @staticmethod
    def _libris_artifact_path(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if not isinstance(value, dict):
            return ''
        return str(value.get('path') or '').strip()

    def _libris_delivery_state(self, swarm: dict[str, Any]) -> dict[str, Any]:
        """Validate the persisted delivery handoff without trusting status alone."""
        raw_manifest = swarm.get('delivery_manifest')
        manifest = raw_manifest if isinstance(raw_manifest, dict) else {}
        raw_artifacts = manifest.get('artifacts')
        artifacts: list[dict[str, str]] = []
        seen_paths: set[str] = set()
        if isinstance(raw_artifacts, list):
            for item in raw_artifacts:
                path = self._libris_artifact_path(item)
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                if isinstance(item, dict):
                    label = str(item.get('label') or item.get('kind') or 'Artifact').strip()
                    media_type = str(item.get('media_type') or '').strip()
                else:
                    label = 'Artifact'
                    media_type = ''
                artifacts.append({
                    'label': label or 'Artifact',
                    'path': path,
                    'media_type': media_type,
                })

        raw_primary = manifest.get('primary_artifact')
        primary_path = self._libris_artifact_path(raw_primary)
        primary: dict[str, str] = {}
        if primary_path:
            if isinstance(raw_primary, dict):
                primary = {
                    'label': str(
                        raw_primary.get('label')
                        or raw_primary.get('kind')
                        or 'Primary artifact'
                    ).strip() or 'Primary artifact',
                    'path': primary_path,
                    'media_type': str(
                        raw_primary.get('media_type') or ''
                    ).strip(),
                }
            else:
                primary = {
                    'label': 'Primary artifact',
                    'path': primary_path,
                    'media_type': '',
                }

        topic_count = manifest.get('topic_count')
        if not isinstance(topic_count, int) or isinstance(topic_count, bool):
            topic_count = 0
        manifest_ready = (
            str(manifest.get('status') or '').strip().lower() == 'ready'
            and manifest.get('ready') is True
            and topic_count > 0
            and bool(primary_path)
            and Path(primary_path).is_absolute()
            and bool(artifacts)
            and all(Path(item['path']).is_absolute() for item in artifacts)
        )
        operation_status = str(swarm.get('status') or 'unknown').strip().lower()
        return {
            'ready': operation_status == 'delivered' and manifest_ready,
            'manifest_valid': manifest_ready,
            'operation_status': operation_status,
            'manifest_status': str(
                manifest.get('status') or 'missing'
            ).strip().lower(),
            'topic_count': topic_count,
            'primary_artifact': primary,
            'artifacts': artifacts,
        }

    @staticmethod
    def _libris_topic_progress(topics: list[dict[str, Any]]) -> str:
        if not topics:
            return 'Topics: none yet'
        complete_states = {
            'checkpointed',
            'ready_high_confidence',
            'plateaued',
            'delivered',
        }
        active_states = {
            'researching',
            'writing',
            'drafting',
            'judging',
            'revising',
            'fanout',
        }
        complete = 0
        active = 0
        blocked = 0
        for topic in topics:
            status = str(topic.get('status') or '').strip().lower()
            if status in complete_states:
                complete += 1
            elif status in active_states:
                active += 1
            elif status:
                blocked += 1
        bits = [f'{len(topics)} total', f'{complete} checkpointed']
        if active:
            bits.append(f'{active} active')
        if blocked:
            bits.append(f'{blocked} blocked/incomplete')
        return 'Topics: ' + ' • '.join(bits)

    def _format_libris_status(self, swarm: dict[str, Any]) -> str:
        op_id = str(swarm.get('operation_id') or '').strip() or '(unknown)'
        status = str(swarm.get('status') or 'unknown').strip().lower()
        topics = [
            topic
            for topic in (swarm.get('topics') or [])
            if isinstance(topic, dict)
        ]
        delivery = self._libris_delivery_state(swarm)
        lines = [
            f'Libris operation: {op_id}',
            f'Status: {status or "unknown"}',
            self._libris_topic_progress(topics),
        ]

        if delivery['ready']:
            primary = delivery['primary_artifact']
            lines.extend([
                '',
                'Complete. Final deliverables are ready.',
                f'Primary artifact ({primary["label"]}):',
                f'  {primary["path"]}',
                '',
                'Artifacts:',
            ])
            for artifact in delivery['artifacts']:
                suffix = (
                    f' [{artifact["media_type"]}]'
                    if artifact.get('media_type')
                    else ''
                )
                lines.append(
                    f'- {artifact["label"]}{suffix}: {artifact["path"]}'
                )
            lines.append('')
            lines.append('Press F4 to inspect the completed agent graph.')
            return '\n'.join(lines)

        if status == 'delivered':
            lines.extend([
                '',
                'Delivery is incomplete.',
                (
                    'The operation is marked delivered, but it does not have a '
                    'valid, non-empty delivery manifest with a primary artifact. '
                    'Do not treat this run as successfully completed.'
                ),
            ])
            return '\n'.join(lines)

        if status in {'failed', 'stopped', 'budget_exhausted', 'idle'}:
            label = {
                'failed': 'The run failed before producing a valid delivery.',
                'stopped': 'The run was stopped before producing a valid delivery.',
                'budget_exhausted': (
                    'The run exhausted its budget before producing a valid delivery.'
                ),
                'idle': 'The run ended without selecting a valid delivery.',
            }[status]
            lines.extend(['', label])
            return '\n'.join(lines)

        if status == 'awaiting_clarification':
            lines.extend([
                '',
                'The run is paused and waiting for clarification.',
            ])
            return '\n'.join(lines)

        if status in {'reports_ready', 'assembling_delivery'}:
            lines.extend([
                '',
                'Research is finished, but final deliverables are still being packaged.',
            ])
            return '\n'.join(lines)

        lines.extend([
            '',
            f'Still running — current stage: {status or "unknown"}.',
            'No final artifact is ready yet.',
        ])
        return '\n'.join(lines)

    def _emit_libris_status(
        self,
        operation_id: str,
        request_id: str | None,
    ) -> bool:
        op_id = str(operation_id or '').strip()
        if not op_id:
            common.emit({
                'type': 'error',
                'error': (
                    'No Libris operation is associated with this session. '
                    'Use /libris status <operation_id>.'
                ),
                'request_id': request_id,
            })
            return False
        try:
            swarm = self._load_libris_swarm(op_id)
        except Exception as exc:
            common.emit({
                'type': 'error',
                'error': f'Libris status failed: {exc}',
                'request_id': request_id,
            })
            return False
        if not swarm:
            common.emit({
                'type': 'error',
                'error': f'No Libris operation found: {op_id}',
                'request_id': request_id,
            })
            return False
        common.emit({
            'type': 'status',
            'message': self._format_libris_status(swarm),
            'request_id': request_id,
        })
        return True

    def _handle_libris_natural_question(
        self,
        text: str,
        request_id: str | None,
    ) -> bool:
        raw = str(text or '').strip()
        lower = raw.lower()
        explicit_libris = bool(re.search(r'\blibris\b', lower))
        operation_id = self._resolve_libris_operation_id(raw)
        has_session_operation = bool(operation_id)
        if not explicit_libris and not has_session_operation:
            return False

        subject = (
            r'(?:it|libris|the\s+(?:libris\s+)?'
            r'(?:research|run|project|operation))'
        )
        completion_patterns = [
            rf'\b(?:did|has)\s+{subject}\s+(?:finish|finished|complete|completed)\b',
            rf'\bis\s+{subject}\s+(?:done|finished|complete|completed)\b',
            rf'\bwhat(?:\'s|\s+is)\s+{subject}(?:\'s)?\s+status\b',
            r'\bwhat(?:\'s|\s+is)\s+(?:the|its|libris)\s+status\b',
            r'\bhow\s+is\s+libris\s+(?:going|doing|progressing)\b',
            r'\blibris\s+status\b',
        ]
        artifact_patterns = [
            (
                r'\bwhere\s+(?:are|is)\s+(?:the\s+)?'
                r'(?:results?|outputs?|artifacts?|reports?|deliverables?)\b'
            ),
            (
                r'\bwhere\s+(?:did|does)\s+(?:it|libris)\s+'
                r'(?:put|save|write)\s+(?:the\s+)?'
                r'(?:results?|outputs?|artifacts?|reports?|deliverables?)\b'
            ),
            (
                r'\b(?:show|find|open)\s+(?:me\s+)?(?:the\s+)?'
                r'(?:results?|outputs?|artifacts?|reports?|deliverables?)\b'
            ),
            (
                r'\bwhere\s+(?:can|should)\s+i\s+(?:find|look\s+for)\s+'
                r'(?:the\s+)?'
                r'(?:results?|outputs?|artifacts?|reports?|deliverables?)\b'
            ),
        ]
        is_question = any(
            re.search(pattern, lower)
            for pattern in (*completion_patterns, *artifact_patterns)
        )
        if not is_question:
            return False

        self._emit_libris_status(operation_id, request_id)
        common.emit({
            'type': 'chat_complete',
            'summary': (
                'Libris status checked for '
                f'{operation_id or "unknown operation"}.'
            ),
            'request_id': request_id,
        })
        return True

    def _maybe_emit_libris_completed(self) -> None:
        announced = getattr(self, '_announced_libris_completions', None)
        if announced is None:
            announced = set()
            self._announced_libris_completions = announced

        tracked = getattr(self, '_tracked_libris_operation_ids', None)
        operation_ids: list[str] = []
        for operation_id in tracked or []:
            op_id = str(operation_id or '').strip()
            if op_id and op_id not in operation_ids:
                operation_ids.append(op_id)

        # Backends created before session tracking existed still need a useful
        # completion notification.  An explicitly empty tracking list belongs
        # to a fresh session and must not replay an unrelated historical run.
        if tracked is None:
            fallback = self._resolve_libris_operation_id()
            if fallback:
                operation_ids.append(fallback)

        for op_id in operation_ids:
            if op_id in announced:
                continue
            try:
                swarm = self._load_libris_swarm(op_id)
            except Exception:
                continue
            delivery = self._libris_delivery_state(swarm)
            if not delivery['ready']:
                continue
            announced.add(op_id)
            common.emit({
                'type': 'libris_completed',
                'operation_id': op_id,
                'status': 'delivered',
                'topic_count': delivery['topic_count'],
                'artifact_count': len(delivery['artifacts']),
                'primary_artifact': delivery['primary_artifact'],
                'artifacts': delivery['artifacts'],
                'request_id': None,
            })

    def _get_refresh_payload(self) -> dict:
        """Augment dashboard refreshes with a one-shot delivery notification."""
        payload = super()._get_refresh_payload()
        self._maybe_emit_libris_completed()
        return payload

    def _emit_libris_intake(self, request_id: str | None) -> None:
        pending = self._pending_libris_intake or {}
        common.emit({
            'type': 'libris_intake',
            'prompt': str(pending.get('prompt') or ''),
            'stop_condition': str(pending.get('stop_condition') or ''),
            'options': [str(option) for option in (pending.get('goal_options') or [])],
            'request_id': request_id,
        })

    def _start_libris_from_pending(self, request_id: str | None) -> None:
        pending = self._pending_libris_intake or {}
        prompt = str(pending.get('prompt') or '').strip()
        if not prompt:
            common.emit({'type': 'error', 'error': 'No pending Libris intake.', 'request_id': request_id})
            return
        selected_goal = str(pending.get('selected_goal') or '').strip()
        stop_condition = str(pending.get('stop_condition') or '').strip()
        budget = self._libris_parse_budget(stop_condition)
        full_prompt = prompt
        if selected_goal:
            full_prompt += f'\n\nResearch goal standard: {selected_goal}'
        if stop_condition:
            full_prompt += f'\n\nStop condition: {stop_condition}'
        try:
            from charon.libris.libris_durable import start_durable_libris_research
            res = start_durable_libris_research(
                common.STATE_DIR,
                Path(self._libris_project_root()),
                prompt=full_prompt,
                parent_agent_id=self._active_agent_id or '',
                budget=budget,
            )
            op = res.get('operation') or {}
            coord = res.get('coordinator') or {}
            operation_id = str(op.get('operation_id') or '').strip()
            self._last_libris_operation_id = operation_id
            if operation_id:
                tracked = getattr(
                    self,
                    '_tracked_libris_operation_ids',
                    None,
                )
                if tracked is None:
                    tracked = []
                    self._tracked_libris_operation_ids = tracked
                if operation_id not in tracked:
                    tracked.append(operation_id)
                announced = getattr(
                    self,
                    '_announced_libris_completions',
                    None,
                )
                if announced is None:
                    announced = set()
                    self._announced_libris_completions = announced
                announced.discard(operation_id)
            self._pending_libris_intake = None
            common.emit({
                'type': 'status',
                'message': (
                    f'Libris research started.\n'
                    f'Operation: {operation_id}\n'
                    f'Coordinator: {coord.get("id")} ({coord.get("name")})\n'
                    f'Durable continuation: {res.get("durable_op_id")}\n'
                    f'Budget: {budget or "(none set)"}\n'
                    'Press F4 to watch the live graph.\n'
                    'Use /libris status to inspect progress and output locations.'
                ),
                'request_id': request_id,
            })
            common.emit({
                'type': 'refresh',
                'payload': self._get_refresh_payload(),
                'request_id': request_id,
            })
            common.emit({
                'type': 'libris_started',
                'operation_id': operation_id,
                'request_id': request_id,
            })
        except Exception as e:
            common.emit({'type': 'error', 'error': f'Failed to start Libris research: {e}', 'request_id': request_id})
            # Keep and re-present the intake so a transient launch failure does
            # not strand the user's selections.
            self._emit_libris_intake(request_id)
