from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from charon.automation.automation_runtime import (
    finalize_run,
    get_automation_state,
    heartbeat_automation,
    list_automations,
    mark_run_started,
    reconcile_stale_automation_runs,
)
from charon.tools import ToolContext

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


_scheduler_lock = threading.Lock()
_scheduler_threads: dict[str, threading.Thread] = {}
_running_automations: set[str] = set()
_continuous_threads: dict[str, threading.Thread] = {}


def _http_check(action: dict[str, Any]) -> tuple[bool, str, dict[str, Any], str]:
    import httpx

    url = str(action.get('url') or '').strip()
    method = str(action.get('method') or 'GET').upper()
    timeout = int(action.get('timeout_seconds') or 20)
    expected_text = str(action.get('expected_text') or '').strip()
    if not url:
        return False, 'Missing URL for HTTP check.', {}, 'missing_url'

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.request(method, url)
        body = resp.text[:4000]
        ok = 200 <= resp.status_code < 400
        if expected_text and expected_text not in body:
            ok = False
            return False, f'HTTP check failed: expected text not found at {url}', {
                'url': url,
                'status_code': resp.status_code,
                'expected_text': expected_text,
                'body_excerpt': body[:500],
            }, 'expected_text_missing'
        summary = f'HTTP {resp.status_code} from {url}' if ok else f'HTTP check failed with status {resp.status_code} for {url}'
        return ok, summary, {
            'url': url,
            'status_code': resp.status_code,
            'body_excerpt': body[:500],
        }, '' if ok else f'http_status_{resp.status_code}'
    except Exception as e:
        return False, f'HTTP check error for {url}: {e}', {'url': url}, str(e)


def _browser_check(doc: dict[str, Any]) -> tuple[bool, str, dict[str, Any], str]:
    from charon.tools.browser_tool import execute_browser

    action = doc.get('action') or {}
    state_dir = Path(str((doc.get('state_dir') or ''))) if doc.get('state_dir') else None
    project_root = Path(str(doc.get('project_root') or '.'))
    ctx = ToolContext(project_root=project_root, state_dir=state_dir, agent_id=str(doc.get('automation_id') or ''))
    url = str(action.get('url') or '').strip()
    expected_text = str(action.get('expected_text') or '').strip()
    screenshot_on_failure = bool(action.get('screenshot_on_failure', True))
    if not url:
        return False, 'Missing URL for browser check.', {}, 'missing_url'

    nav = execute_browser({'action': 'navigate', 'url': url}, ctx)
    nav_text = nav.content or ''
    ok = not nav.is_error and 'browser error:' not in nav_text.lower() and 'error:' not in nav_text.lower()
    details: dict[str, Any] = {'url': url, 'page_state': nav_text[:2000]}
    error = ''
    summary = f'Browser check loaded {url}'

    if expected_text and expected_text not in nav_text:
        ok = False
        error = 'expected_text_missing'
        summary = f'Browser check failed: expected text not found at {url}'
        details['expected_text'] = expected_text
    elif not ok:
        error = 'browser_navigation_failed'
        summary = f'Browser check failed to load {url}'

    if not ok and screenshot_on_failure:
        ss = execute_browser({'action': 'screenshot'}, ctx)
        details['screenshot'] = ss.content[:500]
    return ok, summary, details, error


def _browser_workflow(doc: dict[str, Any]) -> tuple[bool, str, dict[str, Any], str]:
    from charon.tools.browser_tool import execute_browser

    action = doc.get('action') or {}
    steps = list(action.get('steps') or [])
    state_dir = Path(str((doc.get('state_dir') or ''))) if doc.get('state_dir') else None
    project_root = Path(str(doc.get('project_root') or '.'))
    ctx = ToolContext(project_root=project_root, state_dir=state_dir, agent_id=str(doc.get('automation_id') or ''))
    details: dict[str, Any] = {'steps': []}
    screenshot_on_failure = bool(action.get('screenshot_on_failure', True))

    if not steps:
        return False, 'Missing workflow steps for browser workflow.', {}, 'missing_steps'

    for idx, step in enumerate(steps, start=1):
        step_action = str(step.get('action') or '').strip().lower()
        record = {'index': idx, 'action': step_action}
        if step_action == 'assert_text':
            state = execute_browser({'action': 'get_state'}, ctx)
            text = state.content or ''
            expected = str(step.get('text') or step.get('expected_text') or '').strip()
            record['expected_text'] = expected
            record['state_excerpt'] = text[:500]
            details['steps'].append(record)
            if not expected or expected not in text:
                if screenshot_on_failure:
                    ss = execute_browser({'action': 'screenshot'}, ctx)
                    details['screenshot'] = ss.content[:500]
                return False, f'Browser workflow failed at step {idx}: expected text not found.', details, 'workflow_expected_text_missing'
            continue

        params = {'action': step_action}
        for key in ('url', 'index', 'selector', 'text', 'direction', 'seconds'):
            if key in step:
                params[key] = step.get(key)
        result = execute_browser(params, ctx)
        record['result'] = (result.content or '')[:500]
        record['is_error'] = bool(result.is_error)
        details['steps'].append(record)
        content_lower = (result.content or '').lower()
        if result.is_error or content_lower.startswith('error:') or 'browser error:' in content_lower:
            if screenshot_on_failure:
                ss = execute_browser({'action': 'screenshot'}, ctx)
                details['screenshot'] = ss.content[:500]
            return False, f'Browser workflow failed at step {idx}: {step_action}', details, f'workflow_step_{step_action}_failed'

    return True, 'Browser workflow completed successfully.', details, ''


def _execute_automation_kind(doc: dict[str, Any]) -> tuple[bool, str, dict[str, Any], str]:
    kind = str(doc.get('kind') or '')
    action = doc.get('action') or {}
    if kind == 'http_check':
        return _http_check(action)
    if kind == 'browser_check':
        return _browser_check(doc)
    if kind == 'browser_workflow':
        return _browser_workflow(doc)
    return False, f'Unsupported automation kind: {kind}', {'kind': kind}, 'unsupported_kind'


def _run_automation_once(state_dir: Path, automation_id: str) -> None:
    try:
        doc = get_automation_state(state_dir, automation_id)
        if not doc:
            return
        ok, summary, details, error = _execute_automation_kind(doc)
        finalize_run(state_dir, automation_id, ok=ok, summary=summary, details=details, error=error)
    finally:
        with _scheduler_lock:
            _running_automations.discard(automation_id)


def _run_continuous_automation(state_dir: Path, automation_id: str) -> None:
    try:
        while True:
            doc = get_automation_state(state_dir, automation_id)
            if not doc:
                return
            if str(doc.get('status') or '') not in {'active', 'stopping'}:
                return
            if doc.get('stop_requested'):
                if int(doc.get('active_run_count') or 0) <= 0:
                    finalize_run(state_dir, automation_id, ok=True, summary='Automation stopped after stop request.', details={'stopped': True})
                return

            if int(doc.get('active_run_count') or 0) > 0 and (doc.get('execution_policy') or {}).get('skip_if_running', True):
                heartbeat_automation(state_dir, automation_id, 'Continuous automation waiting for current run to finish.')
                time.sleep(max(1.0, float((doc.get('schedule') or {}).get('poll_seconds') or (doc.get('execution_policy') or {}).get('poll_seconds') or 60)))
                continue

            with _scheduler_lock:
                if automation_id in _running_automations:
                    pass
                else:
                    _running_automations.add(automation_id)
                    mark_run_started(state_dir, automation_id)
                    try:
                        ok, summary, details, error = _execute_automation_kind(doc)
                        finalize_run(state_dir, automation_id, ok=ok, summary=summary, details=details, error=error)
                    finally:
                        _running_automations.discard(automation_id)

            refreshed = get_automation_state(state_dir, automation_id)
            poll_seconds = max(
                1.0,
                float((refreshed.get('schedule') or {}).get('poll_seconds') or (refreshed.get('execution_policy') or {}).get('poll_seconds') or 60),
            )
            heartbeat_automation(state_dir, automation_id)
            time.sleep(poll_seconds)
    finally:
        with _scheduler_lock:
            _continuous_threads.pop(automation_id, None)
            _running_automations.discard(automation_id)


def run_due_automations_once(state_dir: Path, *, now_ts: float | None = None) -> list[str]:
    now = time.time() if now_ts is None else float(now_ts)
    started: list[str] = []
    for item in list_automations(state_dir):
        automation_id = str(item.get('automation_id') or '')
        if not automation_id:
            continue
        status = str(item.get('status') or '')
        if status not in {'active', 'stopping'}:
            continue

        if str(item.get('mode') or '') == 'continuous':
            with _scheduler_lock:
                existing = _continuous_threads.get(automation_id)
                if existing and existing.is_alive():
                    continue
                thread = threading.Thread(target=_run_continuous_automation, args=(state_dir, automation_id), daemon=True)
                _continuous_threads[automation_id] = thread
                thread.start()
                started.append(automation_id)
            continue

        if item.get('stop_requested') and int(item.get('active_run_count') or 0) <= 0:
            finalize_run(state_dir, automation_id, ok=True, summary='Automation stopped after stop request.', details={'stopped': True})
            continue
        next_run_ts = float(item.get('next_run_ts') or 0)
        if next_run_ts and next_run_ts > now:
            continue
        if int(item.get('active_run_count') or 0) > 0 and (item.get('execution_policy') or {}).get('skip_if_running', True):
            continue
        with _scheduler_lock:
            if automation_id in _running_automations:
                continue
            _running_automations.add(automation_id)
        mark_run_started(state_dir, automation_id)
        thread = threading.Thread(target=_run_automation_once, args=(state_dir, automation_id), daemon=True)
        thread.start()
        started.append(automation_id)
    return started


def _scheduler_main(state_dir: Path, poll_seconds: float) -> None:
    while True:
        try:
            run_due_automations_once(state_dir)
        except Exception:
            pass
        time.sleep(max(0.5, poll_seconds))


def start_scheduler(state_dir: Path, *, poll_seconds: float = 5.0) -> bool:
    key = str(Path(state_dir).resolve())
    try:
        reconcile_stale_automation_runs(Path(state_dir))
    except Exception as e:
        _diag('automation_scheduler', 'stale automation run reconcile failed on startup', error=e)
    with _scheduler_lock:
        existing = _scheduler_threads.get(key)
        if existing and existing.is_alive():
            return False
        thread = threading.Thread(target=_scheduler_main, args=(Path(state_dir), poll_seconds), daemon=True)
        _scheduler_threads[key] = thread
        thread.start()
        return True


__all__ = ['start_scheduler', 'run_due_automations_once']
