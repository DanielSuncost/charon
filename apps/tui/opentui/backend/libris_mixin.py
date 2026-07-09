"""Libris intake/launch mixin."""
from __future__ import annotations

import re
from pathlib import Path

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

    def _emit_libris_intake(self, request_id: str | None) -> None:
        pending = self._pending_libris_intake or {}
        options = pending.get('goal_options') or []
        prompt = pending.get('prompt') or ''
        stop = pending.get('stop_condition') or '(none set)'
        lines = [
            'Libris intake: before starting, I want to make sure we have a clear research standard.',
            '',
            f'Research prompt: {prompt}',
            f'Stop condition: {stop}',
            '',
            'Suggested research goals:',
        ]
        for i, opt in enumerate(options, 1):
            lines.append(f'{i}. {opt}')
        lines.extend([
            '',
            'Reply with one of:',
            '/libris use 1      choose a suggested goal',
            '/libris use 2',
            '/libris use 3',
            '/libris custom <goal>',
            '/libris stop <condition>',
            '/libris go         start with the current selections',
        ])
        common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})

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
            from libris_agents import start_autonomous_libris_research
            res = start_autonomous_libris_research(
                common.STATE_DIR,
                Path(self._libris_project_root()),
                prompt=full_prompt,
                parent_agent_id=self._active_agent_id or '',
                budget=budget,
            )
            op = res.get('operation') or {}
            coord = res.get('coordinator') or {}
            self._pending_libris_intake = None
            common.emit({
                'type': 'status',
                'message': (
                    f'Libris research started.\n'
                    f'Operation: {op.get("operation_id")}\n'
                    f'Coordinator: {coord.get("id")} ({coord.get("name")})\n'
                    f'Budget: {budget or "(none set)"}\n'
                    f'Use /libris status {op.get("operation_id")} to inspect swarm state.'
                ),
                'request_id': request_id,
            })
            common.emit({'type': 'refresh', 'payload': {'session_info': self._get_session_info()}, 'request_id': request_id})
            common.emit({'type': 'libris_started', 'operation_id': op.get('operation_id'), 'request_id': request_id})
        except Exception as e:
            common.emit({'type': 'error', 'error': f'Failed to start Libris research: {e}', 'request_id': request_id})
