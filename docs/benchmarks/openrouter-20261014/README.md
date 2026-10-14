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

Validation: `tests/test_openrouter_error_body.py`, 3 passing cases covering
nested/long JSON and HTML error bodies, credential redaction, attempt stderr,
diagnostics, metrics, and Edit validation. No broad suite was run.

Resume was not launched: applying this fix changes executed source beyond the
non-negotiable base pin. A recorded compatibility overlay needs explicit
permission; reporting patched source as an unchanged commit would be misleading.
No images were pulled or deleted. The separate testbed PATH defect is untouched.
