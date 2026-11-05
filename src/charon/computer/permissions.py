"""Which macOS privacy grants desktop computer use needs, and who holds them.

TCC (the macOS privacy database) attaches a grant to the code identity of
the *responsible process*. Measured on this machine: under tmux a Python
child is its own responsible process, so without a helper the grant would
fall on Homebrew's interpreter (org.python.python, ad-hoc signed). That
would hand screen capture and input injection to every script the
interpreter runs, and an upgrade would silently drop it. Launched from
Terminal.app the same child is attributed to Terminal instead.

So no Charon Python process holds these grants. They belong to the Charon
Desktop helper (``ai.charon.desktop``, see ``helper.py``), which is signed
with a Developer ID and disclaims responsibility on start, so the grants
are its own whoever launched it (verified under both tmux and Terminal).

Three grants are involved:

* Screen Recording, to capture the screen (perception).
* Accessibility, to post synthetic mouse and keyboard events and to read
  the focused UI element (so a focused password field can be refused).
* Input Monitoring, for the listen-only event tap that pauses the agent on
  the first human keystroke or mouse event (the dead-man's switch).

A missing grant fails silently in the macOS APIs (a black or wallpaper-only
capture, events that go nowhere, a tap that never fires), so every caller
must check ``probe()`` first and refuse with its plain-language message
rather than run degraded. A helper that is absent, unsigned, ad-hoc signed
or unable to take responsibility for itself counts as nothing granted.

Preflight is read-only and never prompts. ``request()`` shows the system
prompts and is only called from an explicit user command. Screen Recording
preflight can stay false until the helper restarts; it is started fresh
for every probe, so a re-check after granting is enough.
"""
from __future__ import annotations

import sys

from charon.computer.helper import DesktopHelper, HelperError, default_app_path

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


def _unavailable(reason: str, app: str = '') -> dict:
    result = {'supported': sys.platform == 'darwin', 'ready': False, 'grants': {g: None for g in GRANTS},
              'missing': list(GRANTS), 'helper': {'app': app}, 'error': reason}
    result['message'] = explain(result)
    return result


def probe(helper: DesktopHelper | None = None) -> dict:
    """Grant status as the helper itself sees it. Pass a running helper to
    reuse it; otherwise a fresh one is started and closed."""
    if sys.platform != 'darwin':
        return {'supported': False, 'ready': False, 'grants': {}, 'missing': list(GRANTS), 'helper': {},
                'error': 'macOS only',
                'message': ['Desktop computer use is macOS only; it is unavailable on this system.']}
    own = helper is None
    helper = helper or DesktopHelper()
    try:
        if own:
            helper.start()
        reply = helper.call('permissions')
    except HelperError as exc:
        return _unavailable(str(exc), str(helper.app))
    finally:
        if own:
            helper.close()
    raw = reply.get('grants') or {}
    grants = {g: (raw.get(g) if isinstance(raw.get(g), bool) else None) for g in GRANTS}
    missing = [g for g in GRANTS if grants[g] is not True]
    result = {'supported': True, 'ready': not missing, 'grants': grants, 'missing': missing, 'error': '',
              'helper': {'app': str(helper.app), 'team': helper.signature.get('team', ''),
                         'bundle_id': helper.identity.get('bundle_id', ''),
                         'version': helper.identity.get('version', '')}}
    result['message'] = explain(result)
    return result


def explain(result: dict) -> list[str]:
    if not result.get('supported'):
        return list(result.get('message') or ['Desktop computer use is macOS only.'])
    grants = result.get('grants') or {}
    app = (result.get('helper') or {}).get('app') or str(default_app_path())
    lines = []
    if result.get('error'):
        lines.append(f'Desktop computer use is OFF: {result["error"]}')
        return lines
    for g in GRANTS:
        mark = '✓' if grants.get(g) is True else '✗'
        lines.append(f'{mark} {PANE[g]} — to {PURPOSE[g]}')
    team = (result.get('helper') or {}).get('team')
    if not result.get('missing'):
        lines.append(f'All granted to Charon Desktop ({app}, team {team}).')
        return lines
    lines.append('Desktop computer use is OFF until these are granted to Charon Desktop:')
    lines.append(f'  {app}')
    lines.append('System Settings → Privacy & Security → each ✗ pane above → + → choose that app '
                 '(or run /persistent access request to have macOS list it for you), then switch it on.')
    lines.append('Grant it to Charon Desktop only, not to Python or your terminal.')
    return lines


def request(helper: DesktopHelper | None = None) -> dict:
    """Ask macOS to show its prompt for each missing grant, which also adds
    Charon Desktop to those panes. Only from an explicit user command."""
    own = helper is None
    helper = helper or DesktopHelper()
    try:
        if own:
            helper.start()
        before = probe(helper)
        for g in before['missing']:
            helper.call('request_access', grant=g)
        return probe(helper)
    except HelperError as exc:
        return _unavailable(str(exc), str(helper.app))
    finally:
        if own:
            helper.close()
