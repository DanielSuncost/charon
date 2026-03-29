from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import os
import re
import subprocess

DEFAULT_AGENT_TARGETS = ("hermes", "opencode", "pi", "openclaw", "claude", "codex", "charon")
PS_COMMAND = ("ps", "-eo", "pid,comm,args")
_MAX_ARGS_DISPLAY = 120
_NO_PROCESSES_MESSAGE = "No tracked agent processes detected."


@dataclass
class RunningAgentProcess:
    target: str
    pid: int
    command: str
    args: str
    has_boat: bool = False

    def formatted_args(self, max_length: int = _MAX_ARGS_DISPLAY) -> str:
        text = (self.args or "").strip()
        if not text:
            return ""
        if len(text) <= max_length:
            return text
        return text[: max_length - 1] + "…"


def _compile_patterns(targets: Sequence[str]) -> dict[str, re.Pattern]:
    return {target: re.compile(rf"\b{re.escape(target)}\b", re.IGNORECASE) for target in targets}


def detect_agent_processes(
    targets: Sequence[str] | None = None,
    ps_cmd: Sequence[str] = PS_COMMAND,
    exclude_self: bool = True,
) -> list[RunningAgentProcess]:
    chosen_targets = list(targets or DEFAULT_AGENT_TARGETS)
    patterns = _compile_patterns(chosen_targets)

    try:
        proc = subprocess.run(ps_cmd, capture_output=True, text=True, check=True)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return []

    lines = proc.stdout.splitlines()
    if len(lines) <= 1:
        return []

    # Exclude our own process and parent
    exclude_pids: set[int] = set()
    if exclude_self:
        exclude_pids.add(os.getpid())
        try:
            exclude_pids.add(os.getppid())
        except Exception:
            pass

    detected: list[RunningAgentProcess] = []
    seen_pids: set[int] = set()

    for raw in lines[1:]:
        if not raw.strip():
            continue
        parts = raw.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid in seen_pids or pid in exclude_pids:
            continue
        command = parts[1]
        args = parts[2] if len(parts) > 2 else ""
        full = f"{command} {args}"
        has_boat = 'charons-boat' in full.lower() or '/.charon/boats/' in full.lower() or 'boat-' in full.lower()
        # Skip internal/helper processes — only detect the main agent process
        if 'chat_backend.py' in full:
            continue
        if 'tmux new-session' in full or 'tmux attach' in full:
            continue
        if command in ('bash', '/usr/bin/bash', '/bin/bash') and 'bun run' in args:
            continue
        for target in chosen_targets:
            pattern = patterns[target]
            if pattern.search(command) or pattern.search(args):
                seen_pids.add(pid)
                detected.append(RunningAgentProcess(target=target, pid=pid, command=command, args=args.strip(), has_boat=has_boat))
                break

    return sorted(detected, key=lambda proc: (proc.target, proc.pid))


def summarize_agent_processes(
    processes: Sequence[RunningAgentProcess],
    *,
    max_args_length: int = _MAX_ARGS_DISPLAY,
) -> list[str]:
    if not processes:
        return [_NO_PROCESSES_MESSAGE]

    lines: list[str] = []
    for proc in sorted(processes, key=lambda item: (item.target, item.pid)):
        args = proc.formatted_args(max_args_length)
        args_suffix = f" {args}" if args else ""
        lines.append(f"{proc.target:<10s} pid={proc.pid:<6d} {proc.command}{args_suffix}")
    return lines


__all__ = [
    "DEFAULT_AGENT_TARGETS",
    "RunningAgentProcess",
    "detect_agent_processes",
    "summarize_agent_processes",
]
