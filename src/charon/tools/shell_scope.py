"""Static analysis of shell commands for the paths they would write.

Shade scope used to be enforced on Read/Write/Edit only; Bash and Git were
waved through with "can't reliably scope bash commands". That made the
README's promise that a shade "is prevented from touching files outside its
contract" untrue, and let a shade in a judge loop rewrite the frozen checker
it was being scored against.

Perfect analysis of shell is impossible and this module does not attempt it.
It splits a command into simple commands, extracts the write targets it can
name with confidence, and refuses to guess at anything else. A command it
cannot read is reported as unanalyzable so the caller can deny it rather than
assume it is harmless. This module only reports; the policy decision belongs
to the caller.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

# Constructs whose effects cannot be known without running them.
_OPAQUE_PATTERNS = (
    (re.compile(r'\$\('), 'command substitution $(...)'),
    (re.compile(r'`'), 'command substitution with backticks'),
    (re.compile(r'<\('), 'process substitution'),
    (re.compile(r'\$\{?[A-Za-z_][A-Za-z0-9_]*\}?'), 'variable expansion'),
)

# Executables that can write anywhere, taking their instructions as data.
_INTERPRETERS = {
    'eval', 'exec', 'source', '.', 'xargs', 'env', 'sudo', 'doas', 'nohup',
    'python', 'python3', 'perl', 'ruby', 'node', 'deno', 'bun', 'php',
    'osascript', 'bash', 'sh', 'zsh', 'ksh', 'dash', 'fish', 'awk', 'gawk',
}

# Commands whose last non-flag argument is the destination.
_DEST_LAST = {'cp', 'mv', 'install', 'rsync', 'ln'}

# Commands where every non-flag argument is written.
_ALL_ARGS_WRITTEN = {
    'rm', 'rmdir', 'unlink', 'shred', 'truncate', 'touch', 'mkdir',
    'chmod', 'chown', 'chgrp', 'chflags',
}

# git subcommands that alter the working tree or index beyond a named path.
_GIT_MUTATING = {
    'checkout', 'restore', 'apply', 'clean', 'stash', 'reset', 'revert',
    'merge', 'rebase', 'cherry-pick', 'pull', 'switch', 'rm', 'mv', 'add',
}

_SEPARATORS = {';', '&&', '||', '|', '&'}
_REDIRECT_OPS = ('>', '>>', '>|', '&>', '&>>')


@dataclass
class ShellScopeAnalysis:
    """What a command was found to write, or why that could not be determined."""

    targets: list[Path] = field(default_factory=list)
    unanalyzable: str | None = None

    @property
    def is_analyzable(self) -> bool:
        return self.unanalyzable is None


def _resolve(raw: str, cwd: Path) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = cwd / p
    try:
        return p.resolve()
    except Exception:
        # An unresolvable path is still a named target. Keep the lexical form
        # so the caller can still refuse it.
        return p


def _split_simple_commands(tokens: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SEPARATORS:
            if current:
                out.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        out.append(current)
    return out


def _strip_flags(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith('-')]


def _scan_simple(argv: list[str], cwd: Path, analysis: ShellScopeAnalysis) -> None:
    if not argv:
        return

    # Redirections write regardless of which executable is being run.
    i = 0
    remaining: list[str] = []
    while i < len(argv):
        tok = argv[i]
        if tok in _REDIRECT_OPS:
            if i + 1 >= len(argv):
                analysis.unanalyzable = 'redirection with no target'
                return
            analysis.targets.append(_resolve(argv[i + 1], cwd))
            i += 2
            continue
        m = re.match(r'^[0-9]*(?:>>|>)(.+)$', tok)
        if m:
            analysis.targets.append(_resolve(m.group(1), cwd))
            i += 1
            continue
        remaining.append(tok)
        i += 1

    argv = remaining
    if not argv:
        return

    exe = Path(argv[0]).name
    args = argv[1:]

    if exe in _INTERPRETERS:
        analysis.unanalyzable = f'{exe} can write arbitrary paths from arguments it interprets'
        return

    if exe == 'git':
        sub = next((a for a in args if not a.startswith('-')), '')
        if sub in _GIT_MUTATING:
            analysis.unanalyzable = f'git {sub} can alter tracked files across the whole repository'
        return

    if exe == 'tee':
        for a in _strip_flags(args):
            analysis.targets.append(_resolve(a, cwd))
        return

    if exe == 'dd':
        for a in args:
            if a.startswith('of='):
                analysis.targets.append(_resolve(a[3:], cwd))
        return

    if exe in ('sed', 'perl') and any(a.startswith('-i') for a in args):
        non_flag = _strip_flags(args)
        for a in non_flag[1:]:
            analysis.targets.append(_resolve(a, cwd))
        return

    if exe in _DEST_LAST:
        non_flag = _strip_flags(args)
        if non_flag:
            analysis.targets.append(_resolve(non_flag[-1], cwd))
        return

    if exe in _ALL_ARGS_WRITTEN:
        for a in _strip_flags(args):
            analysis.targets.append(_resolve(a, cwd))
        return

    if exe in ('tar', 'unzip'):
        for idx, a in enumerate(args):
            if a in ('-C', '-d') and idx + 1 < len(args):
                analysis.targets.append(_resolve(args[idx + 1], cwd))
        return

    # Anything else is treated as read-only. Redirection, captured above, is the
    # channel through which an unrecognised command writes.


def analyze_write_targets(command: str, cwd: Path) -> ShellScopeAnalysis:
    """Report the paths `command` would write, or why that cannot be determined."""
    analysis = ShellScopeAnalysis()
    if not command or not command.strip():
        return analysis

    for pattern, reason in _OPAQUE_PATTERNS:
        if pattern.search(command):
            analysis.unanalyzable = reason
            return analysis

    # shlex drops the operators we need to split on, so tokenise around them.
    tokens: list[str] = []
    for raw in re.split(r'(\|\||&&|;|\||&)', command):
        raw = raw.strip()
        if not raw:
            continue
        if raw in _SEPARATORS:
            tokens.append(raw)
            continue
        try:
            tokens.extend(shlex.split(raw, posix=True))
        except ValueError as exc:
            analysis.unanalyzable = f'could not parse command ({exc})'
            return analysis
        tokens.append(';')

    for argv in _split_simple_commands(tokens):
        _scan_simple(argv, cwd, analysis)
        if analysis.unanalyzable:
            return analysis

    return analysis
