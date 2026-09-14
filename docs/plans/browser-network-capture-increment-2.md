# cap-002 increment 2: redacted traffic capture and HTTP client drafts

Increment 1 supplies explicit attachment, session grants, exact-origin checks,
per-call mutation approval and a fixed CDP operation allowlist. Increment 2 must
extend those boundaries rather than add a raw protocol or request-replay tool.
Estimated implementation: 6–9 engineering days, sequenced as follows.

1. **Capture contract and redaction primitives (1–2 days).** Define an opt-in,
   session-scoped capture ID, retention limits, accepted content types and a
   normalized request/response record. Use a synthetic fixture containing secrets
   in every supported location before adding capture. Redact before storage,
   diagnostics, model context, HAR serialization or draft generation. Drop
   Authorization, Proxy-Authorization, Cookie and Set-Cookie entirely. Deny
   credential/token/session/key/password names case-insensitively in headers,
   query parameters, JSON and form fields, recursively. Replace secret values
   with typed placeholders; never include the original value in hashes or logs.
   Prefer allowlisted useful headers and body fields to a denylist alone. Treat
   unknown bodies, multipart uploads, binary data and unparseable structured
   content as omitted, not raw fallback. URL paths can contain secrets: retain
   route templates or explicitly approved segments; strip userinfo/fragments.
   Test encoded names, duplicate query keys, nested arrays and oversized input.
2. **Bounded capture on the selected target (2 days).** Add named start/status/
   stop operations and fixed CDP subscriptions. Apply the session grant and origin
   checks to each event, redirect hop and response body retrieval. Recheck origin
   before consuming buffered events. Bound duration, request count and bytes;
   retain only redacted data in memory. No capture from sibling tabs or workers.
   Explicitly account for requests already active when capture begins. Starting
   capture requires approval for inspecting potentially sensitive traffic.
   Stop/detach must unsubscribe and clear raw transient buffers, including errors.
3. **Reviewable HAR export (1–2 days).** Export an allowlisted HAR subset with
   omission/redaction markers and capture provenance. Run redaction again at the
   export boundary. Write only within the caller's existing filesystem scope and
   frozen-path rules, using bounded retention and explicit export destinations.
   Do not let browser session access grant filesystem access. Validate with a HAR
   reader and adversarial fixtures; scan artifacts, diagnostics and generated
   prompts for all planted secrets. Do not promise reliable secret detection in
   arbitrary text; default to omission where structured guarantees are absent.
4. **HTTP client drafts (1–2 days).** Generate plain, reviewable Python/httpx drafts
   from a selected redacted request. Credentials come from named environment
   variables and unresolved values from explicit function parameters. Include
   method, route template, permitted headers, structured body, timeout and expected
   response shape. Mark mutating requests and unknown values clearly. Treat
   captured strings as data and escape them through structured code generation;
   never execute captured code. Include a dry-run description and a local fixture
   test. Creation does not run the client; execution/replay requires a separate
   approved tool call and the existing network/mutation policy.
5. **End-to-end acceptance and docs (1 day).** Real Chromium fixture with redirects,
   auth cookies, tokens, failures and download responses. Verify redacted HAR and
   draft output, independent-shade denial, origin transitions, bounded memory,
   detach cleanup and approval denial. Run core-only import checks, Ruff and the
   full suite. Document capture gaps and retention/deletion behavior.

Deliberately excluded: automatic request replay, bulk endpoint discovery, copying
live cookies into generated clients, OAuth refresh flows, authentication bypass,
WebSocket payload capture, binary/multipart bodies, worker/background-tab capture,
full browser-wide tracing, browser-wide download policy, generated SDKs and
production-ready clients. Drafts remain local artifacts requiring review.
