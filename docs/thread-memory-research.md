# Cross-agent thread memory — research program (results, gaps, public-data plan)

Status page for turning the threads layer (`threads.py`, docs/memory-subsystem.md §1.4)
into a research-grade capability. Everything here follows the house rules: objective
structural metrics, no LLM judges, nulls and failures reported next to wins.

## 1. Positioning — why this is the wedge

Nearly all agent-memory work (MemGPT/Letta, Mem0, Zep, LongMemEval, LoCoMo) is
single-agent user↔assistant chat memory. The underexplored problem is
**organizational memory for agent teams**: across many agents working a project,
reconstruct who raised what, who decided, when, and why — decision provenance
("git blame for decisions"). No standard benchmark exists for it. Framing beyond
memory: as agent teams do longer autonomous work, "which agent decided this, on
what rationale, what was rejected" is auditability/oversight infrastructure, not
just recall.

## 2. Results to date (all on-device, structural gold, reproducible)

| Eval | Script | Headline numbers |
|---|---|---|
| Moderate thread benchmark | `exp_thread_reconstruction.py` | coverage 0.94; structural metrics 1.00 |
| Hard: sibling topics, 6 agents, interleaved, noise | `exp_thread_reconstruction_hard.py` | coverage 0.75→0.77, precision 0.38, sibling pull-in 0.10→0.12 |
| Importance-aware ranking (`recall_events(importance_weight=)`) | same, `--importance-weight` sweep | +0.02 coverage at w=0.5 (held-out validated); **w>1 hurts** (promotes other threads' decisions) |
| Decision auto-extraction | `exp_decision_extraction.py` | precision 0.87, recall 0.71, F1 0.78 on a labeled corpus with held-out batch; 0 polarity inversions after negation guard |
| Scale + supersession + flat-RAG baseline | `exp_thread_scale.py` | see below |
| Real traces (46 recorded Charon tasks) | `exp_real_traces.py` | descriptive; 3 capture gaps found (below) |

Scale run (template-generated threads, 5 gold events + 2 noise each, 25% get a
later overriding decision; flat baseline = same texts as plain memories):

| threads | events | thread cov@10 | flat-RAG cov@10 | precision | current-decision acc | stale rate | query ms |
|---|---|---|---|---|---|---|---|
| 24 | ~180 | 1.00 | 1.00 | 0.53 | 0.36 | 0.64 | 14 |
| 96 | ~730 | 0.98 | 0.98 | 0.51 | 0.32 | 0.68 | 16 |
| 288 | ~2,200 | 0.91 | 0.88 | 0.47 | 0.29 | 0.67 | 25 |

### What the numbers honestly say

1. **The broken query class is supersession.** When a decision was later
   overridden, "what is our CURRENT choice" surfaces the stale decision ~2/3 of
   the time, at every scale. Content similarity has no reason to prefer the newer
   decision. This is the concrete, measurable research problem: recency-among-
   relevant ranking / decision version chains (the semantic-store version-chain
   null in `docs/memory-retrieval-eval.md` says naive approaches won't transfer —
   decisions are typed and explicitly superseding, so a scoped chain may do
   better; that's the experiment).
2. **Structure ≈ flat RAG on raw coverage** (0.91 vs 0.88 at 288 threads). An
   honest null: episodic structure does not materially improve retrieval hit-rate.
   Its value is what flat storage cannot answer at all — attribution (WHO),
   typed decisions with rationale (WHY), chronology, supersession semantics —
   plus graceful scaling and ~25ms on-device queries at 2k+ events.
3. **Importance weighting is a small, bounded win.** w=0.5 gives +0.02 coverage
   (validated on held-out seeds); w>1 backfires by promoting all high-importance
   events regardless of thread. Ranking cannot fix content-level thread
   confusion — that needs entity/causal links or query routing.
4. **Auto decision capture works but is conservative by design** (recall 0.71:
   colloquial commitments like "Postgres it is." are missed). The one dangerous
   failure mode — polarity inversion ("we're NOT going to use X" logged as an X
   decision) — is guarded and tested. On 46 real conversational task responses it
   extracted 0 decisions: real chat-style responses rarely state decisions in
   committed language; extraction pays off on longer agentic work sessions, and
   that claim still needs real-trace evidence.

### Real-trace findings (capture gaps, `exp_real_traces.py`)

Replaying the 46 real recorded tasks through the current pipeline exposed three
capture-side gaps that gate ALL of the above on real data:

- **`agent_id` empty in 46/46 records** — the WHO is structurally absent; thread
  attribution is blank on real data until the daemon plumbs agent identity.
- **Deployed daemon predates Phase B** — live state has no episodic tables and
  its records lack `tool_sequence` (155 tool calls recorded as counts only).
  The pipeline exists in code; the deployment needs refreshing.
- **Replay loses original timestamps** — `create_task_episode` stamps now();
  faithful replay needs an injectable ts (small API addition, not yet done).

## 3. Public data: yes — the flatten-then-reconstruct plan

Verified availability (July 2026). Design: ingest a corpus as a FLAT stream of
(ts, actor, text) events with thread identifiers hidden, then grade thread(),
attribution, ordering, and decision queries against the withheld structure.
Gold stays structural — no LLM judge.

| Corpus | Fit | Gold available | Notes |
|---|---|---|---|
| **Wikipedia AfD debates** (ConvoKit `wiki-articles-for-deletion-corpus`) | **Best overall** — the only corpus with all four layers | per-utterance author + unix ts + reply links; **labeled outcome + rationale + deciding admin** | ~400k debates / 3.2M utterances, 2005–2018; GPL-3 data; humans not agents; hundreds of concurrent same-day debates make natural interleaving |
| **GH Archive** (BigQuery / hourly json.gz) | Decision provenance at scale | actor + ISO ts per event; issue/PR number = thread key; merge/close = decision (who+when) | rationale is unlabeled comment text — grade decision retrieval, not rationale matching; GitHub ToS |
| **IRC disentanglement** (Kummerfeld 2019) | Coverage/attribution/ordering | gold reply graph over interleaved multi-party chat; CC-BY-4.0 | ~77k annotated messages; minute-granular ts; no decisions |
| **MAST/MAD traces** (Berkeley) | Realistic multi-agent LLM *input* material | failure-mode labels only; per-agent attribution needs per-framework log parsing | CC-BY-4.0 on HF; ~66MB; no thread/decision gold |
| **Who&When** (HF) | who/when attribution grading on agent failures | gold responsible agent + decisive step + explanation | small (184 tasks); license unstated |
| LongMemEval | existing single-agent baseline only | evidence-session labels | already run; nulls documented |
| LoCoMo | skip | none of the needed structure | 10 dialogs, 2 speakers, CC BY-NC |

Recommended order: **AfD first** (all-gold, huge, directly tests sibling
discrimination via near-duplicate concurrent debates and decision+rationale
retrieval), then **GH Archive** (real engineering decision provenance; the
"why was this merged" query class), IRC as the pure-disentanglement stressor.
Multi-agent realism (MAST/Who&When) after the capture gaps in §2 are fixed, so
Charon's own traces can join them.

## 4. The one valid value study (designed, API-gated, not yet run)

Question: does thread memory help when the decision history CANNOT fit in
context? Setup: corpora from §3 at a scale where the flattened history is ≫
context window; arms = (a) thread()/why() retrieval into a fixed budget,
(b) truncated-context baseline (most recent N tokens), same reader model both
arms; grade answer correctness on decision/attribution questions with
structural gold. Non-confounded because the information genuinely cannot be
in-context (docs/memory-subsystem.md §5); result is bounded by retrieval
recall, which §2 quantifies. Needs an API reader — run when budgeted.

## 5. Priority queue (updated)

1. **Supersession fix + measure** — typed decision chains ("switch X→Y" links to
   the decision it overrides) and recency-among-relevant for decision queries;
   `exp_thread_scale.py` already grades it (current-decision acc 0.29–0.36 → target
   >0.8 without hurting non-superseded queries).
2. **Fix real capture** — plumb `agent_id`, deploy the current daemon build,
   injectable ts for replay. Gates every real-data claim.
3. **AfD flatten-then-reconstruct adapter** — first public-data numbers.
4. GH Archive adapter (decision provenance at engineering scale).
5. Entity/causal links between events (thread graph) — attack the sibling
   pull-in / precision ceiling that ranking cannot fix.
6. Context-overflow value study (§4) when API budget is allocated.
