# Browser attachment and constrained CDP (cap-002, increment 1)

Browser still launches its own Chromium by default. Playwright remains an optional
extra; there is no new dependency or browser automation module. All automation
runs on the existing event-loop thread. Public calls are serialized through one
lock, including approval and attachment transitions.

## Operator setup and attachment

Start Chromium with remote debugging enabled using a dedicated user-data directory.
Configure `CHARON_BROWSER_ALLOWED_ORIGINS` in Charon's **operator environment**, for
example `https://app.example.com,http://127.0.0.1:8000`. Entries are exact HTTP(S)
origins: scheme, hostname and effective port must match. Subdomains and wildcards
are not implicit grants. Missing configuration fails closed. Tool arguments cannot
supply or enlarge this allowlist.

Call Browser with:

```json
{"action":"attach","endpoint":"http://127.0.0.1:9222","url":"https://app.example.com/inbox"}
```

Only loopback HTTP discovery endpoints are supported in this increment. The exact
URL must match one existing page, across all contexts. Zero or multiple matches
fail without selecting a page. Close the tool's launched browser before attaching;
attachment does not silently replace it. The result includes a random `session_id`.
Every subsequent call, including reads and detach, must include that ID. The owner
is the invoking `ToolContext.agent_id`.

```json
{"action":"cdp","session_id":"<returned ID>","operation":"dom_document"}
{"action":"detach","session_id":"<returned ID>"}
```

Attachment and detachment clear the element, frame, snapshot and vision registries
and event histories. Reattachment issues a new session ID, so old calls cannot
address a new page even if element numbering restarts. An attached page that closes
or disconnects fails closed: it never silently launches a replacement.

Detach (also `close` in attached mode) disables request interception, removes our
dialog listener and disconnects the Playwright transport. It does **not** close
user pages, contexts or Chromium. Changes already approved and made to the page
remain. Attach uses the default dialog policy, saves the previous launch policy,
and restores it on detach. Failed attachment also disconnects and clears state.

## Privilege and origin boundaries

Attach and every state-changing attached action require per-call interactive
approval through `infra/tool_approval.py`, even for direct executor callers.
This includes navigation, history, scroll, clicks, input, dialog policy and
detach/close. Inspection, assertions, screenshots, vision, wait, and the read-only
CDP operations do not add an attachment approval prompt. The operator's explicit
`CHARON_SKIP_APPROVAL` override still applies. Generic remembered Browser approvals,
research auto-approval and shade path-scope exemptions cannot grant attachment
privileges. Approval runs within the serialized executor, so session transitions
cannot race classification. Attach approval authorizes event monitoring and the
default automatic dialog responses; later policy changes need their own approval.

A shade (scope/frozen restriction, parent identity, or nonzero topology depth)
cannot attach. A trusted host/overseer must grant an existing session through
`ToolContext.browser_session_grants = [session_id]`. This field is not a Browser
argument, and grants are not automatically inherited by spawned shades. A grant
allows access, not approval bypass. Unscoped callers must also be the owner or
have an explicit grant. Both the registry scope checker and direct executor enforce
these rules. There is no agent-facing tool to mint grants in this increment.

Before attached operations and returned DOM/CDP state, the selected page and all
its current frames must have allowlisted origins. Mixed-origin frames, opaque
origins and internal pages fail closed, including screenshots/vision. CDP Fetch
interception refuses disallowed new requests on the selected page target, including
redirect hops. Charon's extra CDP subscriptions target only the selected page;
no public operation enumerates or switches to sibling tabs. Playwright's browser
connection still discovers existing contexts and pages to locate the target.

This is an automation-access boundary, **not a browser-wide network firewall**.
Existing user traffic, service workers, separate popup/OOPIF targets and browser
extensions are not comprehensively intercepted. Use an isolated profile or external
network policy if browser-wide egress containment is required. A page can change
independently between checks; Charon refuses subsequent inspection when its origin
or frame set is outside the allowlist. It cannot undo activity performed by the
user or page itself. Popup target attachment and worker automation are not offered.

Every attach and CDP attempt (including refusals), plus detach, writes a
`browser_audit` event through diagnostics to the context's state directory. Entries
contain actor, session ID when available, action, operation, and outcome, never
endpoints, URLs, arguments, console content, credentials or response bodies.
Diagnostics remains best-effort as designed by the existing infrastructure; this
is not a tamper-resistant audit sink. Supply a writable `state_dir` for persistence.

## Named operation allowlist

| Operation | Fixed implementation | Purpose |
| --- | --- | --- |
| `dom_document` | `DOM.getDocument(depth=1, pierce=false)` | Shallow DOM structure without arbitrary node/target traversal |
| `network_status` | `Network.getSecurityIsolationStatus` on selected page | Inspect network isolation, without cookies or traffic bodies |
| `console_messages` | bounded `Log.entryAdded` and `Runtime.consoleAPICalled` metadata | Browser logs and primitive console arguments; no remote object inspection |
| `downloads` | bounded `Page.downloadWillBegin` metadata | Suggested filenames observed since attach; no save paths or browser-wide download policy |
| `dialogs` | bounded `Page.javascriptDialogOpening` metadata | Observe dialog type/message since attach |
| `dialog_policy` | existing Playwright dialog policy setter | Approval-gated accept/dismiss policy; optional prompt text |

Only `action`, `session_id`, and `operation` are accepted on CDP calls, plus
`policy`/`text` for `dialog_policy`. Arbitrary methods/parameters are rejected,
including `Runtime.evaluate`, `Runtime.callFunctionOn`, cookie APIs, browser target
creation, filesystem/download configuration and request replay. Internally fixed
`Runtime.enable` only subscribes to console events; no public JS execution exists.
Event storage is bounded to 100 records, text fields to 1,000 characters. These
inspection results can contain page-sensitive text, so they remain session-scoped.
Download events use Chromium's deprecated page-target event: only new downloads
are observed, and compatibility may vary across Chromium versions. No downloading,
network capture, HAR export or client generation is implemented here.

References: [Playwright CDP attachment](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp),
[CDP protocol](https://chromedevtools.github.io/devtools-protocol/).

## Verification environment note

The real-browser fixture starts a separate full Chromium process with a remote
CDP port. In this macOS environment, full headless Chromium did not deliver
animation frames even in a standalone Playwright connection without Charon.
Selector actionability waits timed out there. Approved mutation is verified using
the existing ref-click DOM fallback; the launch and selector implementations are
unchanged. Visible user Chrome and other Chromium versions were not verified.
