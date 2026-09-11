"""A scoped shade must not be able to write outside its contract.

README.md:105 states that each shade "is prevented from touching files outside
its contract". That was only true of Read/Write/Edit: Bash and Git returned
early from _check_scope, so `Bash("echo x > /anywhere")` escaped the scope and
the frozen denylist alike — including the frozen checker a shade is scored
against in a judge loop. These tests hold the promise to its wording.
"""

from pathlib import Path

import pytest

from charon.tools import ToolContext, _check_scope


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    (tmp_path / 'src').mkdir()
    (tmp_path / 'tests').mkdir()
    return ToolContext(
        project_root=tmp_path,
        scope=['src'],
        frozen=['tests'],
    )


def _bash(cmd: str, ctx: ToolContext) -> str | None:
    return _check_scope('Bash', {'command': cmd}, ctx)


# ── the escape the promise forbids ──────────────────────────────────────────

def test_bash_cannot_write_outside_scope(ctx):
    assert _bash('echo pwned > /tmp/escape.txt', ctx) is not None
    assert _bash('echo pwned > ../outside.txt', ctx) is not None


def test_bash_cannot_write_to_a_frozen_path(ctx):
    # The judge-loop case: rewriting the checker you are being scored against.
    err = _bash('echo "assert True" > tests/test_checker.py', ctx)
    assert err is not None
    assert 'Frozen' in err


def test_bash_cannot_reach_outside_scope_by_any_common_write_form(ctx):
    for cmd in (
        'echo x >> /tmp/a.txt',
        'tee /tmp/a.txt',
        'cp src/x.py /tmp/a.py',
        'mv src/x.py /tmp/a.py',
        'rm -rf /tmp/other',
        'sed -i s/a/b/ ../outside.py',
        'dd of=/tmp/a.img',
        'truncate -s 0 /tmp/a.txt',
    ):
        assert _bash(cmd, ctx) is not None, f'escaped via: {cmd}'


def test_unreadable_commands_are_refused_not_assumed_safe(ctx):
    # An unanalysable command is the case most likely to be an escape.
    for cmd in (
        'python -c "open(\'/tmp/a\',\'w\').write(\'x\')"',
        'eval "$CMD"',
        'echo $(whoami) > src/out.txt',
        'bash -c "echo x > /tmp/a"',
    ):
        assert _bash(cmd, ctx) is not None, f'not refused: {cmd}'


# ── and must not become useless in the process ──────────────────────────────

def test_writes_inside_scope_are_allowed(ctx):
    assert _bash('echo x > src/out.txt', ctx) is None
    assert _bash('sed -i s/a/b/ src/x.py', ctx) is None


def test_reads_are_unrestricted(ctx):
    assert _bash('cat tests/test_checker.py', ctx) is None
    assert _bash('ls -la /tmp', ctx) is None
    assert _bash('grep -rn foo src', ctx) is None


def test_unscoped_agents_are_unaffected(tmp_path):
    free = ToolContext(project_root=tmp_path)
    assert _check_scope('Bash', {'command': 'echo x > /tmp/anything'}, free) is None
    assert _check_scope('Git', {'action': 'checkout', 'branch': 'main'}, free) is None


# ── Git ─────────────────────────────────────────────────────────────────────

def test_git_reads_allowed_mutations_gated(ctx):
    for action in ('status', 'diff', 'log', 'branch'):
        assert _check_scope('Git', {'action': action}, ctx) is None

    assert _check_scope('Git', {'action': 'checkout', 'branch': 'other'}, ctx) is not None
    assert _check_scope('Git', {'action': 'stash'}, ctx) is not None


def test_git_add_is_limited_to_scope(ctx):
    assert _check_scope('Git', {'action': 'add', 'files': ['src/x.py']}, ctx) is None
    assert _check_scope('Git', {'action': 'add', 'files': ['tests/t.py']}, ctx) is not None
    # The default is `.`, which would stage the whole repository.
    assert _check_scope('Git', {'action': 'add'}, ctx) is not None
