# Memory subsystem — handoff (status, architecture, honest scope, roadmap)

Read this first if you're picking up the memory work. It's the single source of
truth for what exists, what it honestly is (and isn't), how to run everything, and
what's left to do. Everything described here is **on `master`** unless noted.

## 0. Orientation & house rules

- **Don't overclaim.** Each capability below has a stated boundary; keep claims
  welded to those boundaries. A perfect score (1.00 / 0.00) is a *red flag* that the
  test is too easy or floored, not a win — investigate before celebrating.
- **Objective metrics over LLM judges** for anything value-shaped.
- Run the suite before and after changes: it should stay green.
  ```
  PYTHONPATH=apps/core-daemon CHARON_EMBED_BACKEND=local python -m pytest tests/ -q
  ```
  (~790 passing, 1 skipped. `CHARON_EMBED_BACKEND=local` = in-process embeddings.)

## 1. What the subsystem is

Four layers, all live on `master`:

1. **Semantic / scope-tiered store** (`memory_engine.py`) — the base. On-device
   hybrid retrieval: bge-base + sqlite-vec vectors + FTS5, fused with RRF. Memories
   are organized **by scope** via a `tier` field: `user` / `project` / `agent`
   (NOT by cognitive type). ~10ms recall, no cloud.
2. **Episodic** (`episodic.py`) — first-class `Episode` (one per session/task) with
   time bounds and `source_agent`; **typed sub-events** (`EVENT_TYPES`:
   user_message/agent_message/tool_call/tool_result/decision/observation/
   system_notification); time-structural retrieval (recent / range / before-after)
   and event-level retrieval (`recall_events`).
3. **Procedural** (`procedural.py`) — `Procedure` (goal + steps + success/failure
   counts): `learn_procedure`, `recall_procedures` (relevance × success-rate),
   `record_outcome`, `distill_from_episodes`.
4. **Cross-agent threads** (`threads.py`) — the coordination layer: `thread(topic)`
   and `why(topic)` reconstruct, across ALL agents, the when/who/why of a topic;
   `log_decision` captures a decision with rationale.

**Runtime integration** (`execution_memory.py::create_task_episode`): on task
completion, promotes the task into an Episode (`get_or_create_episode_for_session`,
one per session) and derives typed events (objective→user_message, tool calls→
tool_call, response→agent_message). Fully inside a try/except — cannot break task
completion.

**Agent surface** (`tools/timeline_tool.py`, registered as `Timeline`): actions
`recent | range | topic | events | procedures | thread | why | log_decision`.
Semantic recall is the separate `Recall` tool (`tools/recall_tool.py`).

## 2. Honest scope — what each layer is NOT (keep these welded to any claim)

- **Tiers are by scope (user/project/agent), not semantic/episodic/procedural type.**
- **Episodic is task-granular and completion-derived**, not per-turn streamed: events
  are reconstructed from the recorded task data at completion and share the task
  timestamp — no intermediate reasoning, no sub-turn timing. (Per-turn live capture
  is Phase B-max, unbuilt — see roadmap.)
- **Cross-agent thread reconstruction: coverage 0.94 moderate / 0.75 hard.** The
  moderate benchmark (6 separated topics, 3 agents, no noise) is retrieval-bound at
  0.94; the hard benchmark (12 near-duplicate sibling threads, 6 agents, interleaved
  timelines, distractor noise) drops to **coverage 0.75, precision 0.38, sibling
  pull-in 0.10** — low-importance noise chatter outranks gold implement/verify
  events (`recall_events` ignores event importance; see §4.5). Attribution/ordering
  score 1.00 but are **structural** (deterministic once retrieved) — "correctly
  implemented," not a hard capability. `why()` top-1 is 1.00 on the hard set but
  with only 12 decision candidates (one near-duplicate distractor per query) —
  a coarse discrimination check, not a hard capability.
- **Decision capture: explicit `log_decision` + conservative auto-extraction.**
  `decision_extract.py` (wired into task completion, `auto=True` in details)
  measures P 0.87 / R 0.71 on a labeled corpus with a post-freeze held-out batch
  (`exp_decision_extraction.py`); negated commitments are rejected outright
  (polarity-inversion guard). On 46 real conversational responses it extracted 0
  decisions — value on real agentic sessions is NOT yet demonstrated.
- **Supersession is broken (measured, unfixed):** when a decision was later
  overridden, "current choice" queries surface the stale decision ~2/3 of the
  time at every scale tested (`exp_thread_scale.py`: current-decision acc
  0.29–0.36). Do not claim threads answer "what are we using now."
- **Structure ≈ flat RAG on raw coverage** (0.91 vs 0.88 at 288 threads) — an
  honest null; structure's value is attribution/why/typing/chronology, which
  flat storage cannot answer at all. Query latency ~25ms at 2k+ events.
- **Real capture gaps gate all real-data claims:** live records have empty
  `agent_id` (no WHO), the deployed daemon predates Phase B (no episodic tables,
  no tool_sequence), and replay can't inject original timestamps. See
  `docs/thread-memory-research.md` §2.
- **Procedural memory: structure built and tested; VALUE is not claimed.** A value
  test is confound-prone (see §5) and none is currently asserted. Do not claim
  procedural memory improves task success.
- **Retrieval honest nulls stand:** hybrid+RRF ≡ vector-only on LongMemEval;
  version-chain update-detection gives no retrieval gain; multi-session recall is
  weak (~0.27). See `docs/memory-retrieval-eval.md`.

## 3. Tests & evals (all on-device, no API unless noted)

Tests: `tests/test_episodic.py`, `test_episodic_integration.py`, `test_threads.py`,
`test_procedural.py`, `test_memory_engine.py`, `test_memory_bridge.py`,
`test_checkpoint_manager.py`.

Evals (`scripts/`, run with `PYTHONPATH=apps/core-daemon CHARON_EMBED_BACKEND=local`):
- `exp_memory_ablation.py` — retrieval-mode + version-chain ablations (the nulls).
- `memeval_gen.py` + `exp_memeval.py` — synthetic multi-session recall@k.
- `exp_episodic_queries.py` — temporal "when/where" query benchmark (episodic vs flat;
  timestamp-derived gold).
- `exp_thread_reconstruction.py` — cross-agent thread reconstruction (structural gold).
- `exp_thread_reconstruction_hard.py` — hardened variant (§4.1): near-duplicate
  sibling topics, 6 agents, interleaved timelines, distractor events; adds
  precision + sibling-pull-in + why-discrimination metrics, grades by exact
  (episode_id, summary) keys recorded at construction.
- `exp_memeval_episodic.py` — episode-summary retrieval lift (mixed/small).
- `exp_decision_extraction.py` — decision auto-extraction P/R on a labeled corpus
  (held-out batch policy documented in the script).
- `exp_thread_scale.py` — threads at 24/96/288 scale + decision supersession +
  flat-RAG baseline + latency.
- `exp_real_traces.py` — descriptive replay of real recorded tasks (no gold).

Research program, public-data plan, and consolidated results:
`docs/thread-memory-research.md`.

## 4. Roadmap — things we want to do (roughly ordered by value)

1. **Cross-agent threads — harden the benchmark & capture.**
   - ~~Harder eval: overlapping/near-duplicate topics, interleaved threads, more agents,
     distractor events within a thread → a real (lower) coverage number.~~ **DONE**
     (`exp_thread_reconstruction_hard.py`): coverage 0.75, precision 0.38, sibling
     pull-in 0.10. Main failure mode: low-importance noise outranks gold events —
     feeds directly into §4.5.
   - ~~Automatic decision extraction from agent output.~~ **DONE** (conservative
     heuristic: `decision_extract.py`, P 0.87 / R 0.71, polarity-guarded,
     importance-gated, wired into `create_task_episode`). Classifier upgrade
     still open if real-trace recall proves too low.
   - Causal/entity links (this decision → that change; topic↔entity), so threads
     become a graph, not just a time-sorted list.
   - **NEW, top priority — supersession:** decision chains ("switch X→Y" links
     the decision it overrides) + recency-among-relevant for decision queries.
     Measured broken: stale decision served ~2/3 of the time
     (`exp_thread_scale.py`). Target current-decision acc >0.8 without
     regressing non-superseded queries.
   - **NEW — fix real capture** (gates all real-data claims): plumb `agent_id`
     through task completion, deploy the current daemon build, add injectable
     ts to `create_task_episode` for faithful replay.
2. **NL→time-range parser** ("last Tuesday", "before the refactor" → date range) so
   `Timeline` temporal/thread queries accept natural phrasing. Thin LLM step; measure
   parse accuracy separately from retrieval.
3. **Episodic Phase B-max (per-turn live capture).** Tap the `ConversationEngine`
   event stream to persist intermediate agent messages, tool *results* with timing,
   and system notifications as typed events — importance-gated, batched, fully
   try/except-contained (hot loop; treat with care). This is what closes the
   task-granular → per-turn gap.
4. **Episodic Phase C–E.** Event-rollup episode summaries; richer event-level
   retrieval; an event-granular benchmark to demonstrate the finer granularity.
5. **Retrieval improvements** (motivated by the nulls): confidence-weighted or
   query-routed fusion (equal-RRF doesn't help); **recency-among-relevant** ranking
   (naive global recency backfired — boosts recent distractors; `recall(recency_weight=)`
   exists but is global). Measured so far: `recall_events(importance_weight=0.5)`
   is a small validated win (+0.02 hard-benchmark coverage, held-out seeds);
   weights >1 BACKFIRE by promoting other threads' high-importance events —
   ranking alone cannot fix content-level thread confusion (§4.1 links are the
   structural attack).
6. **The one valid memory *value* study (context-overflow).** Does retrieval help
   when the needed info CANNOT fit the context window (multi-session)? End-to-end QA:
   memory vs truncated-context baseline; needs an LLM reader (API). Non-confounded
   because the info genuinely can't be in-context; result is bounded by recall.

## 5. Methodological cautions (why some "obvious" experiments are traps)

Measuring whether memory *improves task performance* is confound-prone; a valid
study must avoid all three:
- **Floored baseline.** If the task is impossible without the supplied info (baseline
  ≈ 0), you're measuring "we handed over necessary info," not "memory helps." Use a
  genuinely solvable task with a pitfall (baseline ~40–70%).
- **Legibility confound.** "Distilled procedure (explicit) vs raw example (buried)"
  measures explicit-vs-inferred, not procedural memory. Match legibility across arms.
- **ICL reduction.** For a capable model, "does a relevant reminder in context help"
  is trivially yes. The procedural-memory-*specific* value lives in retrieval
  discrimination and outcome reinforcement, or in the context-overflow regime
  (§4.6) — not in "did the supplied rule help."

## 6. Branch/handoff mechanics

- Everything is on `master`. Development happened on the `episodic-memory` branch and
  was fast-forward-merged; that branch currently equals `master`.
- To continue: branch from `master`, keep the suite green, and FF-merge (or PR) back.
- New agent's first three steps: (1) read this doc; (2) run the suite; (3) skim
  `episodic.py`, `threads.py`, `procedural.py`, and `execution_memory.py::create_task_episode`.
