# Context Budget and Completion Recovery (V1)

Status: accepted
ADR: 0001 (cleanroom rules)

Scope: the conversation engine's turn loop
(`src/charon/conversation/conversation_engine.py`), the context budget
(`src/charon/context/context_budget.py`), overflow detection
(`src/charon/providers/context_overflow.py`) and tool-call argument handling
in the OpenAI-compatible and Anthropic providers.

Provenance: the failure modes covered here were collected from Charon's own
benchmark runs and from reading how other harnesses describe the same
problems, including atomic-agent (MIT). Behaviour only was taken as input;
the rules, constants and code below are Charon's own.

## Principles

- A request that ran no tools may be sent again. Nothing else is replayed.
- A recovery the caller does not need to act on is not reported as an
  `error` event. It gets its own event.
- Every recovery is bounded. When the bound is reached the engine behaves
  as it did before this contract existed.
- Stored messages are never rewritten to fit a window. Fitting is done on
  the copy that is sent.
- A repair may restore structure. It may never supply content.

## 1. Context budget

The window is planned as four parts:

    window = overhead + history + reply reserve + safety margin

| Part | Value |
|---|---|
| overhead | estimated tokens of the system prompt and the tool schemas |
| reply reserve | `min(max_tokens, max(1024, window / 4))` |
| safety margin | 256 |
| history | the remainder, at least 512 |

Estimates are characters / 4.

Rules

- History is assembled against the history budget, not the window.
- Compaction starts at `min(threshold * window, history budget)`. With the
  default threshold of 0.7, a model whose window is large against its
  prompt and output cap compacts where it did before.
- The output cap sent with a request is the configured `max_tokens` while
  that fits beside the prompt, otherwise the room left, and never less
  than the reply reserve.
- A single tool result is shown to the model at no more than half the
  history budget. A larger result is sent as its head (30%) and tail (70%)
  around a marker stating how many characters were omitted. The assembler
  costs the result at the clipped size.
- The fresh tail is protected from eviction only while it fits the
  history budget. A larger tail gives up its oldest items. The newest item
  is always sent.
- A window of 0 means unknown: no budgeting, clipping or compaction is
  done, as before.

## 2. Refused prompt (context overflow)

Detection is by provider message (`detect_context_overflow`). Statuses 401,
403, 404 and 429 are never overflows. A provider error helper that sees an
overflow sets `error_code='context_overflow'` and `retryable=False`.

On an overflow, before any text or tool call was received:

1. The planning window shrinks: to the window the provider named, when
   that is at least 1024 and smaller than the current one; otherwise to
   80% of the current one, floor 1024.
2. The new window is recorded for `(provider, model_id)` for the life of
   the process. A recorded window only moves down. `ModelInfo` is not
   modified.
3. History is compacted once, regardless of the trigger, when
   `auto_compact` is on.
4. `context_overflow_recovery` is emitted with `previous_window`, `window`,
   `reported_window`, `reported_prompt_tokens`, `compacted`.
5. The same step is sent again. It does not count toward `max_turns`.

Bound: 2 recoveries per submission. After that, or when the window cannot
shrink further, the provider's error is emitted as an `error` event and the
turn ends as for any other provider error.

The summarizer request made by compaction is itself bounded to the window
it relieves. If the summarizer fails or returns nothing, the summary is a
placeholder followed by the opening request of the summarized span,
verbatim, up to 2000 characters.

## 3. Tool-call arguments

A provider yields `ToolCall.invalid_reason` when the arguments it received
cannot be trusted. The engine never executes such a call. Its result is a
synthetic error carrying the reason, so every emitted call still has
exactly one result. Valid calls in the same completion run normally.

OpenAI-compatible provider, in order:

1. The text parses as a JSON object: used as is.
2. The completion ended at an output limit (`finish_reason` of `length`,
   `max_tokens` or `max_output_tokens`), or the text ends inside a string:
   invalid, "cut off". No repair, no re-ask.
3. Only closing brackets or braces are missing: they are added and the
   result is used.
4. Otherwise, with a schema: one schema-constrained re-ask of the same
   server.
5. Otherwise: invalid, "not valid JSON".

Anthropic provider: tool input that does not parse as an object is invalid.

## 4. Cut-off and empty completions

Applies to a completion with no tool calls, no error, no abort and no
pending steering message.

| Completion | Response | Bound |
|---|---|---|
| ended at an output limit, with text | keep the partial reply, add a notice asking to continue from where it stopped, run another step | 2 in a row |
| ended at an output limit, no text | discard it, add a notice asking for less reasoning, run another step | shares the bound above |
| no text and no tool call | discard it, add a notice that nothing happened, run another step | 1 in a row |

- Notices are `user` messages prefixed `[charon]`.
- A discarded completion is not stored. `message_end` is still emitted
  for it.
- `completion_recovery` is emitted with `kind` (`truncated` or `empty`),
  `turn` and `output_tokens`, followed by `turn_end`.
- A completion that did not end at an output limit resets the first bound.
  A completion with any text or tool call resets the second.
- At the bound the turn ends as before: the partial or empty assistant
  message is stored and `turn_end` carries the provider's stop reason.

The Anthropic provider reports the stream's real `stop_reason`.

## Events added

| Event | Data |
|---|---|
| `context_overflow_recovery` | `turn`, `previous_window`, `window`, `reported_window`, `reported_prompt_tokens`, `compacted` |
| `completion_recovery` | `kind`, `turn`, `output_tokens` |

`turn_end.stop_reason` may now be `empty_completion`, or the provider's
length stop reason, on a step that is followed by another step.

## Not covered

- Loop and stagnation detection (`LoopGuard`).
- Prompt layout for cache stability.
- Per-tool output shaping beyond the single-result cap.
- Persisting learned windows across processes.
