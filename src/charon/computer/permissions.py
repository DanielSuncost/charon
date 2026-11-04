"""Which macOS privacy grants desktop computer use needs, and who holds them.

TCC (the macOS privacy database) attaches a grant to the code identity of
the *responsible process*, not to the program that asked for it and not to
"Charon" as a product. Which process is responsible depends on how the
caller was launched, so it is measured here, in the process that will make
the calls, never assumed:

* Under tmux (how Charon normally runs) the tmux server is detached from any
  app, and a Python started beneath it is its own responsible process. The
  grant must go to the interpreter binary itself: for a venv over Homebrew
  Python that is ``.../Python.framework/.../Resources/Python.app``, which
  ``.venv/bin/python`` re-execs into. Measured on this machine.
* Started directly from a terminal window, the terminal app (Terminal,
  iTerm, Ghostty, ...) is responsible and the grant must go to it.

A probe run in one process says nothing about another with a different
launch chain, which is why the persistent runner probes itself at start and
records the result, and why a probe from the chat backend is labelled as
such.

Three grants are involved:

* Screen Recording, to capture the screen (perception).
* Accessibility, to post synthetic mouse and keyboard events and to read
  the focused UI element (so a focused password field can be refused).
* Input Monitoring, for the listen-only event tap that pauses the agent on
  the first human keystroke or mouse event (the dead-man's switch).

A missing grant fails silently in the macOS APIs (a black or wallpaper-only
capture, events that go nowhere, a tap that never fires), so every caller
must check ``probe()`` first and refuse with its plain-language message
rather than run degraded.

Preflight is read-only and never prompts. ``request()`` shows the system
prompt and is only called from an explicit user command. Screen Recording
preflight can stay false until the granted process restarts; the message
says so.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

SCREEN_RECORDING = 'screen_recording'
ACCESSIBILITY = 'accessibility'
INPUT_MONITORING = 'input_monitoring'
GRANTS = (SCREEN_RECORDING, ACCESSIBILITY, INPUT_MONITORING)

PANE = {
    SCREEN_RECORDING: 'Screen & System Audio Recording',
    ACCESSIBILITY: 'Accessibility',
    INPUT_MONITORING: 'Input Monitoring',
}
PURPOSE = {
    SCREEN_RECORDING: 'see the screen',
    ACCESSIBILITY: 'move the mouse, type, and check for password fields',
    INPUT_MONITORING: 'pause the instant you touch the keyboard or mouse',
}


# ── Responsible process ─────────────────────────────────────────────

def _responsible_pid(pid: int) -> int | None:
    """The pid TCC attributes this process's requests to. Uses the libSystem
    responsibility API (present since 10.14, not in public headers)."""
    try:
        lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        fn = lib.responsibility_get_pid_responsible_for_pid
    except (OSError, AttributeError):
        return None
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_int]
    r = fn(pid)
    return r if r > 0 else None


def _executable_of(pid: int) -> str | None:
    try:
        lib = ctypes.CDLL('/usr/lib/libproc.dylib')
        buf = ctypes.create_string_buffer(4096)
        n = lib.proc_pidpath(ctypes.c_int(pid), buf, ctypes.c_uint32(4096))
        if n > 0:
            return buf.value.decode('utf-8', 'replace')
    except (OSError, AttributeError):
        pass
    return None


def _bundle_of(path: str) -> str | None:
    for parent in Path(path).parents:
        if parent.suffix == '.app':
            return str(parent)
    return None


def _signature(path: str) -> dict:
    """Signing facts that decide whether a grant survives: TCC pins an
    ad-hoc signed binary by its exact hash, so any rebuild or upgrade
    silently voids the grant."""
    try:
        out = subprocess.run(['codesign', '-dv', path], capture_output=True, text=True, timeout=5).stderr
    except (OSError, subprocess.SubprocessError):
        return {}
    info = {}
    for line in out.splitlines():
        key, _, value = line.partition('=')
        if key == 'Identifier':
            info['identifier'] = value
        elif key == 'TeamIdentifier':
            info['team'] = '' if value == 'not set' else value
        elif key == 'Signature':
            info['adhoc'] = value == 'adhoc'
    if 'adhoc' not in info and 'flags=0x2(adhoc)' in out:
        info['adhoc'] = True
    return info


def responsible_process(pid: int | None = None) -> dict:
    pid = os.getpid() if pid is None else pid
    rpid = _responsible_pid(pid) or pid
    exe = _executable_of(rpid) or (sys.executable if rpid == os.getpid() else '')
    bundle = _bundle_of(exe) if exe else None
    target = bundle or exe
    sig = _signature(target) if target else {}
    return {
        'pid': pid,
        'responsible_pid': rpid,
        'is_self': rpid == pid,
        'executable': exe,
        'bundle': bundle,
        'grant_target': target,
        'name': Path(bundle).stem if bundle else (Path(exe).name if exe else 'unknown'),
        'identifier': sig.get('identifier', ''),
        'team': sig.get('team', ''),
        'adhoc': bool(sig.get('adhoc')),
        'interpreter': _is_interpreter(exe),
    }


def _is_interpreter(exe: str | None) -> bool:
    name = Path(exe or '').name.lower()
    return name.startswith('python') or name in ('node', 'ruby', 'perl', 'bash', 'zsh', 'sh')


# ── Grants ──────────────────────────────────────────────────────────

def _preflight() -> dict:
    """Read-only checks. A value of None means the check itself could not
    run, which callers treat as not granted."""
    out: dict = {g: None for g in GRANTS}
    try:
        import Quartz
    except Exception:
        return out
    try:
        out[SCREEN_RECORDING] = bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        pass
    try:
        from ApplicationServices import AXIsProcessTrusted
        ax = bool(AXIsProcessTrusted())
        post = bool(Quartz.CGPreflightPostEventAccess())
        out[ACCESSIBILITY] = ax and post
    except Exception:
        pass
    try:
        out[INPUT_MONITORING] = bool(Quartz.CGPreflightListenEventAccess())
    except Exception:
        pass
    return out


def probe(*, where: str = 'this process') -> dict:
    """Grant status as seen by the calling process. ``where`` names that
    process for the message ('the persistent runner', 'the chat backend')."""
    if sys.platform != 'darwin':
        return {'supported': False, 'where': where, 'grants': {}, 'missing': list(GRANTS),
                'ready': False, 'responsible': {},
                'message': ['Desktop computer use is macOS only; it is unavailable on this system.']}
    grants = _preflight()
    missing = [g for g in GRANTS if grants.get(g) is not True]
    resp = responsible_process()
    result = {'supported': True, 'where': where, 'grants': grants, 'missing': missing,
              'ready': not missing, 'responsible': resp}
    result['message'] = explain(result)
    return result


def explain(result: dict) -> list[str]:
    if not result.get('supported'):
        return list(result.get('message') or [])
    resp = result.get('responsible') or {}
    grants = result.get('grants') or {}
    name = resp.get('name') or 'unknown'
    target = resp.get('grant_target') or 'unknown'
    lines = []
    for g in GRANTS:
        mark = '✓' if grants.get(g) is True else '✗'
        lines.append(f'{mark} {PANE[g]} — to {PURPOSE[g]}')
    if not result.get('missing'):
        lines.append(f'All granted to {name} ({target}), checked from {result.get("where")}.')
        return lines
    lines.append(f'Desktop computer use is OFF until these are granted. macOS checks {result.get("where")} as: {name}')
    lines.append(f'  {target}')
    lines.append('Grant it in System Settings → Privacy & Security → each pane above (use + and pick that path).')
    if grants.get(SCREEN_RECORDING) is not True:
        lines.append('After granting Screen Recording, restart Charon; macOS reports it only to a fresh process.')
    if resp.get('interpreter'):
        lines.append(f'Note: {name} is a general interpreter. A grant to it applies to every script it runs, '
                     'not only Charon.')
    if resp.get('adhoc'):
        lines.append(f'Note: {name} is ad-hoc signed, so macOS ties the grant to this exact build; '
                     'an upgrade or rebuild drops it without warning.')
    return lines


def request(grant: str) -> bool | None:
    """Show the macOS prompt for one grant. Only from an explicit user
    command; returns the preflight result afterwards (often still False
    until the user acts in System Settings)."""
    if sys.platform != 'darwin' or grant not in GRANTS:
        return None
    import Quartz
    if grant == SCREEN_RECORDING:
        Quartz.CGRequestScreenCaptureAccess()
    elif grant == INPUT_MONITORING:
        Quartz.CGRequestListenEventAccess()
    elif grant == ACCESSIBILITY:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
        Quartz.CGRequestPostEventAccess()
    return _preflight().get(grant)
