# OpenRouter host reproduction, 2026-10-14

Run: `charon-swebench-verified-openrouter-haiku55-50x3-20261014`.
Base source: `5a686bb201616bf192eba437c60d5c7a3df47b55`.

A direct host HTTP request reproduced the HTTP 400, without Docker or the
benchmark harness. The payload was captured from the pinned provider using
MockTransport, the pinned engine's system prompt/tool builders with `/testbed`
as cwd, an isolated empty state directory, and the preserved first-instance
`prompt.txt`. The model was `anthropic/claude-haiku-5.5`, context window 200000,
`reasoning_effort: "medium"`, `max_tokens: 32768`, streaming with usage enabled.
The original request bytes were not preserved by the failed container; this
reconstructed its first-request inputs using the pinned code and saved prompt.
The key was read from its external file and never written to artifacts.

`response-400.json` preserves the response verbatim. All attempted upstreams
rejected Edit's top-level `anyOf`. Removing only that combinator from the same
payload returned HTTP 200 and completed a tool-call response. The recorded
usage/cost is preserved in `response-200-usage.json`: $0.0006432. No tool was
executed. Neither reasoning field nor token budget was changed for this probe.
A successful response verifies acceptance of the field, not the provider's
internal reasoning behavior; this response reported zero reasoning tokens.

The permanent fix removes the incompatible schema constraint, documents the
alternatives in the tool description, and preserves existing Edit runtime
validation. OpenAI-compatible HTTP errors now retain the complete decoded body
in provider metrics and diagnostics. With the benchmark's existing
`CHARON_EMIT_REQUEST_CONFIG=1`, a `provider_error` JSON event also persists it
in the attempt's stderr. A reflected API key is redacted. Other provider
implementations and in-stream HTTP-200 error events are outside this change.

Product follow-up: Edit explicitly checks for oldText+newText or edits before
file access and returns an actionable error. Ast's existing _paths handler
already enforces exactly one of path or paths before file access; its top-level
oneOf was removed and the requirement documented in its description. The Ast
implementation was present as uncommitted workspace work, absent at the old
benchmark pin; only Ast and its registration were carried into this branch.
Other pending IDE work was not included.

The complete registered-tool payload (58 tools, no failed optional imports)
returned HTTP 200 with $0.0025643 spend; see full-tools-response-200-usage.json.
The registry/dynamic-loader sweep found zero remaining top-level anyOf/oneOf/
allOf. The only remaining combinator was Edit.edits.items.anyOf. It was present
in both successful probes, confirming nested anyOf is accepted. The raw 400
explicitly rejects top-level oneOf, allOf, or anyOf; the actual first failing
schema was Edit.anyOf. No provider-specific conversion is used.

Validation: tests/test_openrouter_error_body.py, 9 passing cases covering
full error telemetry and redaction, actionable Edit/Ast contract validation,
and the entire registered schema set. No broad suite was run.

The user authorized re-pinning the same run to this product fix. The separate
testbed PATH defect is untouched. Host probe cost is separate from benchmark
estimated spend. A 200 response does not independently prove internal model
reasoning behavior (the response reported zero reasoning tokens).
