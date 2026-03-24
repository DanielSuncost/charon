"""Intelligent task summarization.

Instead of truncating the raw LLM response, this module produces concise
structured summaries from the actual execution record (tool calls, results,
errors). The summary captures what was done, not what the agent said.

Two modes:
1. Fast (no LLM): extract facts from tool calls and build a template summary
2. Rich (LLM): send execution trace to a model for natural language distillation

Fast mode is used by default. Rich mode runs async in the background after
the task completes, upgrading the summary without blocking.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def summarize_fast(
    *,
    instruction: str,
    tool_calls: list[dict],
    response_text: str,
    errors: list[str],
    total_turns: int,
) -> str:
    """Fast summary from execution facts. No LLM needed.

    Produces a concise one-paragraph summary like:
    "Fixed auth bug: edited apps/auth/login.py, ran pytest (5 passed), wrote new test file."
    """
    parts = []

    # What was asked
    instr_short = instruction.strip()[:100]
    if instr_short:
        parts.append(instr_short)

    # What tools did
    files_read = []
    files_written = []
    files_edited = []
    commands_run = []
    command_results = []

    for tc in tool_calls:
        tool = tc.get('tool', '')
        args = tc.get('arguments', tc.get('args', {}))
        result = tc.get('result', '')
        is_error = tc.get('is_error', False)

        if tool == 'Read':
            path = args.get('path', '')
            if path:
                files_read.append(_short_path(path))
        elif tool == 'Write':
            path = args.get('path', '')
            if path:
                files_written.append(_short_path(path))
        elif tool == 'Edit':
            path = args.get('path', '')
            if path:
                files_edited.append(_short_path(path))
        elif tool == 'Bash':
            cmd = args.get('command', '')
            if cmd:
                cmd_short = cmd.strip()[:60]
                commands_run.append(cmd_short)
                if result and not is_error:
                    # Extract key info from command output
                    result_short = _extract_command_highlight(cmd, result)
                    if result_short:
                        command_results.append(result_short)

    # Build action summary
    actions = []
    if files_edited:
        actions.append(f'edited {_join_paths(files_edited)}')
    if files_written:
        actions.append(f'wrote {_join_paths(files_written)}')
    if files_read and not files_edited and not files_written:
        actions.append(f'read {_join_paths(files_read)}')
    if commands_run:
        actions.append(f'ran {len(commands_run)} command(s)')
    for cr in command_results[:2]:
        actions.append(cr)

    if errors:
        err_short = errors[0][:80]
        actions.append(f'error: {err_short}')

    if not actions:
        # No tool calls — use response text
        if response_text:
            return response_text[:200]
        return f'Completed in {total_turns} turn(s)'

    action_text = '; '.join(actions)

    # Combine: instruction + actions
    if instr_short and len(instr_short) < 60:
        return f'{instr_short} → {action_text}'
    return action_text


def _short_path(path: str) -> str:
    """Shorten a path to just filename or last 2 components."""
    parts = path.replace('\\', '/').split('/')
    if len(parts) <= 2:
        return path
    return '/'.join(parts[-2:])


def _join_paths(paths: list[str], max_show: int = 3) -> str:
    """Join paths into a concise string."""
    if len(paths) <= max_show:
        return ', '.join(paths)
    return f'{", ".join(paths[:max_show])} +{len(paths) - max_show} more'


def _extract_command_highlight(command: str, output: str) -> str:
    """Extract the most informative line from command output."""
    cmd_lower = command.lower()
    lines = [l.strip() for l in output.strip().splitlines() if l.strip()]
    if not lines:
        return ''

    # pytest: look for the summary line
    if 'pytest' in cmd_lower:
        for line in reversed(lines):
            if 'passed' in line or 'failed' in line or 'error' in line:
                return line[:80]

    # make/build: look for success/error
    if any(k in cmd_lower for k in ('make', 'build', 'npm', 'bun', 'cargo')):
        for line in reversed(lines):
            if any(k in line.lower() for k in ('error', 'success', 'built', 'compiled', 'bundled')):
                return line[:80]

    # git: summary line
    if 'git' in cmd_lower:
        for line in lines[:3]:
            if line and not line.startswith('#'):
                return line[:80]

    # Default: last non-empty line
    return lines[-1][:80] if lines else ''


async def summarize_rich(
    *,
    instruction: str,
    tool_calls: list[dict],
    response_text: str,
    errors: list[str],
    total_turns: int,
    provider: Any,
    model: Any,
) -> str:
    """Rich summary using LLM. Call asynchronously after task completes.

    Takes the execution trace and distills it into a 1-2 sentence summary.
    """
    from providers import StreamDelta

    # Build trace text
    trace_lines = [f'Task: {instruction[:200]}']
    for tc in tool_calls:
        tool = tc.get('tool', '')
        args = tc.get('arguments', tc.get('args', {}))
        is_error = tc.get('is_error', False)
        if tool == 'Read':
            trace_lines.append(f'  Read: {args.get("path", "")}')
        elif tool == 'Write':
            trace_lines.append(f'  Write: {args.get("path", "")} ({len(args.get("content", ""))} chars)')
        elif tool == 'Edit':
            trace_lines.append(f'  Edit: {args.get("path", "")}')
        elif tool == 'Bash':
            cmd = args.get('command', '')[:80]
            status = '✗' if is_error else '✓'
            trace_lines.append(f'  Bash {status}: {cmd}')
    if errors:
        trace_lines.append(f'  Errors: {"; ".join(e[:60] for e in errors[:3])}')
    trace_lines.append(f'  Turns: {total_turns}, Tool calls: {len(tool_calls)}')

    trace_text = '\n'.join(trace_lines)

    prompt = (
        'Summarize this task execution in 1-2 concise sentences. '
        'Focus on what was actually done (files changed, commands run, outcomes), '
        'not what was planned or discussed. Include specific file names.\n\n'
        f'{trace_text}'
    )

    text_parts = []
    try:
        async for delta in provider.stream(
            messages=[{'role': 'user', 'content': prompt}],
            model=model,
            system_prompt='Output only the summary, nothing else. Be concise.',
            max_tokens=256,
        ):
            if hasattr(delta, 'type') and delta.type == 'text':
                text_parts.append(delta.text)
    except Exception:
        return summarize_fast(
            instruction=instruction, tool_calls=tool_calls,
            response_text=response_text, errors=errors, total_turns=total_turns,
        )

    result = ''.join(text_parts).strip()
    return result[:300] if result else summarize_fast(
        instruction=instruction, tool_calls=tool_calls,
        response_text=response_text, errors=errors, total_turns=total_turns,
    )
