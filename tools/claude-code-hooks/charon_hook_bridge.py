#!/usr/bin/env python3
"""Bridge between Claude Code hooks and Charon's room/event system.

Claude Code calls this script for each hook event. It reads JSON from stdin,
dispatches to the appropriate handler, and writes to Charon's file-based state.

Environment (set by session launcher):
    CHARON_STATE_DIR  — path to .charon_state/
    CHARON_ROOM_ID    — room ID for this Claude Code session

Usage (configured in .claude/settings.local.json):
    charon_hook_bridge.py session-start
    charon_hook_bridge.py user-prompt-submit
    charon_hook_bridge.py pre-tool-use
    charon_hook_bridge.py post-tool-use
    charon_hook_bridge.py stop
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Allow importing Charon core modules.
_SRC = Path(__file__).resolve().parent.parent.parent / 'src'
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from charon.agents.inter_agent_rooms import (  # noqa: E402
    append_event,
    consume_injections,
    load_room,
)


def _read_stdin() -> dict:
    try:
        return json.loads(sys.stdin.read())
    except Exception:
        return {}


def _resolve_context() -> tuple[Path, str]:
    """Return (state_dir, room_id) from environment."""
    state_dir = os.environ.get('CHARON_STATE_DIR', '')
    room_id = os.environ.get('CHARON_ROOM_ID', '')
    if not state_dir or not room_id:
        sys.exit(0)  # silently pass through if not in a Charon-managed session
    return Path(state_dir), room_id


def _truncate(text: str, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + '...'


def _collect_injections(state_dir: Path, room_id: str) -> list[dict]:
    """Consume pending injections targeted at the claude-code participant."""
    room = load_room(state_dir, room_id)
    if not room:
        return []
    participants = room.get('participants') or []
    cc_participant = next(
        (p for p in participants if p.get('role') == 'implementer'),
        None,
    )
    return consume_injections(
        state_dir,
        room_id,
        speaker_role='implementer',
        participant=cc_participant,
    )


# ── Handlers ────────────────────────────────────────────────────────


def handle_session_start(hook_data: dict) -> None:
    state_dir, room_id = _resolve_context()
    room = load_room(state_dir, room_id)
    if not room:
        sys.exit(0)

    meta = room.get('meta') or {}
    task = meta.get('task_instruction', '')

    # Gather any pre-queued injections.
    injections = _collect_injections(state_dir, room_id)
    parts = []
    if task:
        parts.append(f'[Charon task] {task}')
    for inj in injections:
        parts.append(f'[Charon] {inj.get("message", "")}')

    append_event(state_dir, room_id, {
        'type': 'session_started',
        'claude_session_id': hook_data.get('session_id', ''),
    })

    if parts:
        out = json.dumps({'additionalContext': '\n\n'.join(parts)})
        sys.stdout.write(out)
    sys.exit(0)


def handle_user_prompt_submit(hook_data: dict) -> None:
    state_dir, room_id = _resolve_context()

    append_event(state_dir, room_id, {
        'type': 'user_prompt',
        'prompt_preview': _truncate(hook_data.get('user_prompt', ''), 200),
    })

    injections = _collect_injections(state_dir, room_id)
    if injections:
        messages = [inj.get('message', '') for inj in injections if inj.get('message')]
        if messages:
            out = json.dumps({'additionalContext': '\n\n'.join(
                f'[Charon] {m}' for m in messages
            )})
            sys.stdout.write(out)
    sys.exit(0)


def handle_pre_tool_use(hook_data: dict) -> None:
    state_dir, room_id = _resolve_context()

    tool_name = hook_data.get('tool_name', '')
    tool_input = hook_data.get('tool_input', {})
    if isinstance(tool_input, dict):
        input_preview = _truncate(json.dumps(tool_input, ensure_ascii=False), 300)
    else:
        input_preview = _truncate(str(tool_input), 300)

    append_event(state_dir, room_id, {
        'type': 'tool_start',
        'tool_name': tool_name,
        'input_preview': input_preview,
    })
    sys.exit(0)


def handle_post_tool_use(hook_data: dict) -> None:
    state_dir, room_id = _resolve_context()

    tool_name = hook_data.get('tool_name', '')
    tool_output = hook_data.get('tool_result', '')
    if isinstance(tool_output, dict):
        output_preview = _truncate(json.dumps(tool_output, ensure_ascii=False), 300)
    else:
        output_preview = _truncate(str(tool_output), 300)

    append_event(state_dir, room_id, {
        'type': 'tool_complete',
        'tool_name': tool_name,
        'output_preview': output_preview,
    })
    sys.exit(0)


def handle_stop(hook_data: dict) -> None:
    state_dir, room_id = _resolve_context()

    append_event(state_dir, room_id, {
        'type': 'turn_complete',
        'stop_reason': hook_data.get('stop_reason', ''),
    })

    # Check for immediate injections (steering from Charon operator).
    injections = _collect_injections(state_dir, room_id)
    immediate = [
        inj for inj in injections
        if inj.get('when') == 'immediate' and inj.get('message')
    ]
    if immediate:
        feedback = '\n\n'.join(inj['message'] for inj in immediate)
        sys.stderr.write(feedback)
        sys.exit(2)  # block — Claude Code re-engages with feedback

    # Optional judge gate.
    room = load_room(state_dir, room_id)
    meta = (room or {}).get('meta') or {}
    if not meta.get('judge_on_stop'):
        sys.exit(0)

    eval_cmd = meta.get('judge_eval_command', '')
    target_score = float(meta.get('judge_target_score', 1.0))
    if not eval_cmd:
        sys.exit(0)

    # Run eval command, parse last float from output.
    try:
        result = subprocess.run(
            eval_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=meta.get('project', None),
        )
        output = (result.stdout + '\n' + result.stderr).strip()

        # Parse score: last float-like token in output.
        score = None
        for token in reversed(output.split()):
            try:
                score = float(token)
                break
            except ValueError:
                continue

        if score is not None and score < target_score:
            append_event(state_dir, room_id, {
                'type': 'judge_gate_blocked',
                'score': score,
                'target': target_score,
                'eval_output_preview': _truncate(output, 400),
            })
            feedback = (
                f'[Charon judge] Score {score} below target {target_score}.\n'
                f'Eval output:\n{_truncate(output, 800)}\n\n'
                f'Please fix the issues and try again.'
            )
            sys.stderr.write(feedback)
            sys.exit(2)

        if score is not None:
            append_event(state_dir, room_id, {
                'type': 'judge_gate_passed',
                'score': score,
                'target': target_score,
            })

    except subprocess.TimeoutExpired:
        append_event(state_dir, room_id, {
            'type': 'judge_gate_timeout',
            'eval_command': eval_cmd,
        })
    except Exception:
        pass  # don't block on judge errors

    sys.exit(0)


# ── Dispatch ────────────────────────────────────────────────────────

HANDLERS = {
    'session-start': handle_session_start,
    'user-prompt-submit': handle_user_prompt_submit,
    'pre-tool-use': handle_pre_tool_use,
    'post-tool-use': handle_post_tool_use,
    'stop': handle_stop,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in HANDLERS:
        sys.exit(0)  # unknown event — pass through
    hook_data = _read_stdin()
    HANDLERS[sys.argv[1]](hook_data)


if __name__ == '__main__':
    main()
