# Charon Desktop (`ai.charon.desktop`)

The one process that holds Charon's macOS desktop grants (Screen Recording,
Accessibility, Input Monitoring) and the supervision kernel that decides every
desktop action. Python talks to it over a socket pair it authenticates by code
signature (`src/charon/computer/helper.py`). This is host control: it is not
sandboxed.

> **Typed text can arrive wrong in some apps, and that is not a security
> failure.** `type_text` sends each character as a Unicode string on a
> placeholder key (virtual key 0, which is "A" on a US layout). Most apps read
> the string. An app that reads the key code instead receives the placeholder:
> typing `hello` there produces `aaaaa`, or whatever the placeholder key is in
> the current layout. If you see repeated letters you did not ask for, this is
> the cause: the helper typed nothing other than what was requested, into the
> field it was shown. Not yet tested on a real app (needs the grants).

## Build and test

```sh
CHARON_DESKTOP_SIGN_IDENTITY=<Developer ID SHA-1> apps/desktop-helper/build.sh
apps/desktop-helper/test.sh                       # kernel and pure parts, no grants needed
./.venv/bin/python -m pytest tests/test_computer_permissions.py tests/test_computer_destination.py
```

Then grant all three panes in System Settings → Privacy & Security to
`build/desktop/CharonDesktop.app`.

## What the agent can do

Every action needs a time-boxed grant the user approved in the helper's own
dialog, for one action class and one app, and passes the never-approvable
guards (`Sources/DesktopFamily.swift`) first.

| Class | Verb | Notes |
| --- | --- | --- |
| `desktop.observe.screen` | `capture` | Target app's windows only; password fields blacked out; local model only (`src/charon/computer/destination.py`) |
| `desktop.input.pointer` | `pointer_click`, `pointer_scroll` | Names a pixel of a held screenshot |
| `desktop.input.text` | `type_text` | Text fields only; no control characters; 500 characters at most |
| `desktop.input.shortcut` | `press_shortcut` | Fixed allowlist (`Sources/Input/KeyboardCore.swift`) |
| `desktop.input.destructive` | none | Refused at grant time until a verb exists |

## Known behaviour

* **A held button or key after a stop is deliberate.** If the kill switch or a
  re-check stops a sequence between a button's (or key's) down and up, the up
  is not posted: posting it would complete a click or keystroke after the
  stop. The button or key then reads as held until your next real click or key
  press releases it.
* Redaction hides password fields only. Anything else visible in the granted
  app is in the screenshot; the grant dialog says so.
* The capture destination is a Python-side rule and a claim the helper
  records, not a boundary the helper enforces. See the docstring of
  `destination.py` for what it does and does not stop.
