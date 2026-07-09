# Cross-agent decision & discussion threads — the when / who / why

A coordination feature for a *team* of agents: across every agent working in a
project, reconstruct what was discussed and decided about a topic — **by whom,
when, and why** — and let the user (or an agent) query it. Most agent memory is
single-agent and fact-shaped; this is cross-agent and event-shaped, and it's the
thing that makes Charon's multi-agent model actually navigable.

## What it does

- **Capture the WHY** — `Timeline log_decision {what, why, alternatives, topic}`
  records a decision as a typed, attributed, timestamped event (rationale included).
- **Thread the WHEN/WHO/WHAT** — `Timeline thread <topic>` assembles every related
  event **across all agents** (not siloed), chronologically, attributed to the
  owning agent: *"Mar 3 (planner) raised it · Mar 7 (architect) decided JWT **because**
  stateless scales across the fleet · Mar 12 (implementer) shipped it."*
- **Explain the WHY** — `Timeline why <topic>` returns the decision(s), rationale,
  alternatives considered, who decided, and the discussion that led up to it.

## How it's built (on the episodic layer already on master)

- Episodes now carry `source_agent` (the WHO); typed events carry the discussion
  (`user_message`/`agent_message`/`decision`). Threads span agents by querying the
  shared project container, then attributing each event via its episode's agent.
- Decisions are captured with structured rationale (`threads.log_decision`),
  timestamped to their session so they order correctly.
- One episode per session (`get_or_create_episode_for_session`) so a mid-session
  decision and the task-completion record converge instead of duplicating.
- Surfaced through the existing `Timeline` tool. 8 unit/integration tests; full
  suite green (789).

## Objective evaluation (`scripts/experiments/exp_thread_reconstruction.py`)

The feature comes with a clean, non-confounded benchmark: build a multi-agent
scenario with KNOWN threads (each topic raised by one agent, decided by another with
a known rationale, shipped by a third, over time), with other topics as distractors;
then reconstruct each thread and grade against the construction. No LLM judge, no
baseline games — gold is structural. Queried by **paraphrase** ("how do users log
in") rather than the topic keyword, so retrieval must work on meaning (3 seeds × 6
topics):

| metric | score | what it measures |
|---|---|---|
| coverage (gold agents retrieved) | **0.94** | genuine, retrieval-bound — the real number |
| attribution (right agent) | 1.00 | structural (from `episode.source_agent`) |
| ordering (chronological) | 1.00 | structural (thread sorts by time) |
| why recall (rationale surfaced) | 1.00 | structural (stored + retrieved) |

**Honest reading:** only **coverage (0.94)** is a capability number — it's bounded by
semantic retrieval, and it drops from a trivially-perfect 1.00 (keyword query, a mere
wiring check) to 0.94 under paraphrase, i.e. recall occasionally misses an event when
asked by description. The 1.00s are **structural**: once an event is retrieved, its
agent / time / rationale are deterministic, so those scores mean "correctly
implemented," not "hard capability passed." I'm not claiming them as more than that.

## Honest limits

- Synthetic, moderate difficulty: 6 distinguishable topics, 3 events/thread, no
  interleaving or near-duplicate topics. A harder benchmark (overlapping topics,
  interleaved threads, many agents, distractor events within a thread) would stress
  coverage further — that's the next hardening, and the score would be lower.
- Coverage is retrieval-bound, so it inherits the memory engine's recall behavior.
- Decision capture is via the explicit `log_decision` path; automatic extraction of
  decisions from agent output is not implemented.

## Why it matters

It's a genuinely differentiating, working feature — cross-agent coordinated memory
with provenance and rationale — and it ships with an objective eval rather than a
confounded one. It lets you ask your fleet of agents "when and why did we decide X,
and who decided it," and get a navigable answer.
