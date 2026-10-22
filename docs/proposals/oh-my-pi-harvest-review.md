# oh-my-pi vs Charon: harvest-source review

Reviewed: 2026-10-08. Source: local `~/Projects/oh-my-pi`, commit `75bcc219d0`; Charon baseline `0f70466`. Both working trees were clean at inspection. This is a static source/documentation review plus a local scanner experiment, not a runtime benchmark or full test-suite validation.

## Decision

**Recommend adding oh-my-pi as a separate `/harvest_souls` source, after giving it registration-aware extraction.** It is a Pi fork, but its execution, editing, IDE, and evaluation subsystems justify independent tracking. Do not replace Charon's runtime, import the entire stack, or count features shared with pi-mono as independent capability gaps.

This review does not register the repository, queue adoptions, modify the generated registry, pull the checkout, or run provider-backed assimilation.

## Architectural difference

Charon is organized around persistent identities, user/project/agent memory, scoped shade contracts, retained workers, remote fleet coordination, durable graph execution, and research/judge workflows. oh-my-pi is organized around a deeply integrated coding session: model-specific tool contracts, semantic code tools, programmable execution, session trees, subagents, and rich terminal/editor integration.

This is a difference of emphasis, not an exclusive division. OMP also has optional persistent memory, agent lifecycle management, review/advisor machinery, autoresearch code, and resumable benchmark jobs. Charon also has sophisticated local editing, browser use, tool discovery, and a persistent Python kernel. Neither “OMP has no memory” nor “Charon needs a REPL” would be an accurate gap assessment.

OMP's main integration surface is TypeScript/Bun with substantial Rust native code. Charon's runtime is Python with a Rust TUI. Most candidates should be adapted as contracts and behavior, not copied wholesale with their runtime dependencies.

## Capability comparison and priorities

| Area | OMP evidence | Charon overlap / delta | Recommendation |
|---|---|---|---|
| Programmable execution | `docs/tools/eval.md`, `docs/python-repl.md`, `src/eval/` under coding-agent | Charon already retains Python state, soft-interrupts timeouts, calls shades and peers through `charon.rlm`, retains children, and can promote findings. OMP adds JS, general `await tool.*`, kernel-defined tools for children, async handles/workpools, rich MIME capture, and optional background cells. | **High:** general tool bridge and structured async handles first; JS later if needed. |
| Structured child output | `src/eval/agent-bridge.ts`, `src/task/structured-subagent.ts`, `src/tools/yield.ts` | Charon's RLM result is a status/id envelope around the final phase summary, not caller-defined typed task data. | **High:** optional output schema plus explicit valid/invalid status. |
| LSP | `src/lsp/tool.ts`, `client.ts`, `diagnostics.ts`, `edits.ts` | No first-class LSP tool found in Charon's current source/tool registry. OMP handles definitions, references, diagnostics, renames, code actions, and file-rename notifications. | **High:** start read-only and with freshness-aware post-edit diagnostics; semantic mutations later. |
| Structural editing | `src/tools/ast-grep.ts`, `ast-edit.ts`, `docs/tools/ast-edit.md` | Charon has text search, symbol outlines, exact/range edits, version checks, and atomic per-file hunks; it does not expose equivalent first-class structural query/rewrite tools. | **High:** bounded AST queries, preview, stale-check, explicit apply. |
| Edit contract design | `src/edit/`, `crates/pi-edit/`, `docs/tools/edit.md` | Charon already has `Read` versions and `Edit.baseHash`; stale protection itself is not new. OMP adds selectable model-specific edit modes, syntax-block operations, registers, and compact previews. | **Experiment:** benchmark against existing range+hunk edits before adopting a new syntax. |
| Debugger | `src/tools/debug.ts`, `src/dap/` | Shell-driven debugging is possible in Charon; no equivalent first-class DAP tool found. OMP has launch/attach, breakpoints, stepping, threads, scopes, variables and memory operations. | **Medium/high:** useful for native/runtime faults; approval and process ownership matter. |
| Isolated workers | `src/task/isolation-runner.ts`, `worktree.ts` | Charon already has shade scopes and overseer worktree instructions. OMP has explicit isolation setup, captured patches, apply/merge controls, and recovery artifacts. | **High/medium:** managed isolation lifecycle, not merely “add subagents.” |
| Online advisor | `docs/advisor-watchdog.md`, `src/advisor/` | Charon has overseers, judges, and critique workflows. OMP's incremental reviewer of the live primary transcript is a distinct interaction pattern. | **Medium:** opt-in, read-only, bounded cost and note cadence. |
| Stream rule interruption | `docs/ttsr-injection-lifecycle.md`, `src/session/ttsr-coordinator.ts`, `src/export/ttsr.ts` | Distinct from static standing instructions or post-hoc judges. OMP can interrupt/retry on scoped regex rules, and check AST conditions before tool execution. | **Experimental:** test false positives and retry storms; not a security boundary. |
| Unified resource addressing | `src/internal-urls/`, `docs/tools/read.md` | Charon already reads local documents and discovers tools. OMP lets ordinary file-shaped operations address artifacts, subagent output, skills, PRs, and other schemes. | **Medium:** borrow artifact/result addressing; preserve authorization distinctions. |
| Memory | `docs/memory.md`, memory-backend implementations | OMP memory is **off by default**; local summaries/lessons differ from Hindsight/Mnemopi structured memory. Charon's persistent three-tier memory is core. | **Overlap:** study lifecycle/redaction/compaction details rather than replace memory. |
| Evaluation | `packages/metaharness/README.md`, `packages/typescript-edit-benchmark/`, `packages/snapcompact/` | Charon has gym/IPMS, memory experiments, routing calibration, and judge loops. OMP unifies edit/compaction/Harbor experiments, traces, costs, and resumable benchmark runs. | **High methodological value:** matched experiments for each proposed harvest. |
| Collaboration / ACP / native utilities | `docs/collab.md`, ACP tests, `docs/native-crates.md` | Useful integration breadth, but less central to Charon's single-user persistent-agent design. | Defer unless a concrete editor, sharing, portability, or latency need appears. |

OMP source paths in the table beginning `src/` are relative to `packages/coding-agent/`.

## Particularly useful REPL delta

Charon's existing bridge in `src/charon/tools/_pykernel_worker.py` intentionally routes spawning through the normal SpawnShade implementation. Preserve this principle when generalizing it: expose approved tool dispatch, not a parallel implementation that bypasses scopes, topology budgets, or approval checks.

Suggested sequence:

1. `await charon.tools.<name>(...)` or an equivalent explicit dispatcher, using the current ToolContext and normal authorization.
2. Caller-defined structured child results alongside the existing textual summary.
3. Handles with `status`, `wait`, `cancel`, and resumable identifiers, keeping job lifetime independent of a cell wait.
4. Kernel-local functions exposed as narrowly scoped child tools, with explicit ownership and revocation.
5. Rich output/background-cell polish, and a JS kernel only if actual workloads justify another runtime.

OMP's session kernels do **not** restore live variable state after a fresh-process resume. Its documentation also distinguishes active compute time from waits that pause the cell watchdog. If borrowed, these semantics need to be explicit and independently budgeted rather than allowing an unbounded child wait accidentally.

## Caveats from implementation, not just README claims

- Current hashline uses tagged file snapshots and line/block patch operations. It is not sufficient to describe the current implementation as merely a short hash on each individual line. Charon already supports non-retyping line-range edits with a file version.
- OMP's `yield.ts` has a `MAX_SCHEMA_RETRIES` escape hatch that can eventually accept non-conforming data and report `schemaOverridden`. Typed return paths therefore require the parent to inspect validation status. Charon should fail explicitly or return an invalid-result envelope, not silently treat this as valid data.
- OMP's edit documentation explicitly says multi-section writes can leave an already-written prefix after an OS write failure. Prevalidation is not a cross-file transaction.
- `applyEditsThenRename()` restores reference edits when mkdir/rename fails, but its preceding reference-edit loop is outside that try/catch. Do not generalize its guarantee to every possible intermediate write failure.
- Worker isolation is controlled by settings and per-spawn requests; do not infer all workers are automatically isolated from README wording.
- Advisors are read-only by default but can be granted mutating tools. They are not an independent security approval system.
- Advertised benchmark improvements are upstream claims on particular models/tasks, not demonstrated OMP-over-Charon gains. No performance or pass-rate benchmark was reproduced here.
- Root license is MIT. Preserve notices for copied/substantial code and inspect component/third-party licenses before reuse; a root license does not eliminate dependency obligations.

## What the current harvester would do

`src/charon/memory/assimilation.py` currently registers hermes-agent, pi-mono, openclaw, and prime-agent. `_repo_path()` already prefers `~/Projects/<name>`, so local discovery would need no redesign for oh-my-pi.

A direct, read-only call to `_scan_generic_tools()` against `packages/coding-agent/src/tools` produced:

- **161** entries;
- **1** nonempty description;
- **no** `edit`, `lsp`, `task`, or `web_search` entry.

The generic scanner recursively treats TS filenames as tools, includes helper modules, and reads only the first 500 characters for descriptions. OMP's real factory map is `BUILTIN_TOOLS` in `packages/coding-agent/src/tools/index.ts:563`; several important implementations live outside the tools directory. There are also hidden tools, eval preludes, and architecture capabilities that aren't ordinary tool registrations.

Other existing limitations:

- `_scan_architecture()` keeps only the first 3,000 characters per document. The OMP README begins with branding/install material, so this misses most capability content.
- The collected architecture text is stored in results but **not supplied to the first-pass `llm_analyze_abilities()` call**. Merely listing better documents will not make that call discover advisor/TTSR/eval sub-capabilities.
- Non-Hermes skill and command extraction currently returns empty lists.
- A filtered full assimilation generates documents/state from its selected results. Preserve/merge the existing peer inventory rather than accidentally replacing it with a one-repo view.
- Discovery currently tries `git pull` on user-owned checkouts; this review bypassed discovery and did not update either repository. Prefer an explicit read-only/update choice for future scans.

## Proposed registration and extractor

Register `oh-my-pi` with upstream `https://github.com/can1357/oh-my-pi.git`, TypeScript as its primary language, and a dedicated scanner selector. Preserve `pi-mono` separately and track their shared lineage during gap clustering.

The adapter should:

1. Extract actual factory-map registrations (including explicit hidden/conditional status), not all `.ts` files. Fail visibly on layout drift or an empty expected map.
2. Resolve source imports so `edit`, `lsp`, `task`, and `web_search` retain correct source provenance.
3. Attach descriptions from tool docs/prompts; never import or execute untrusted upstream code during scanning.
4. Keep runtime features separately from tools: eval bridge, kernel-defined tools, advisor, TTSR, isolation, URI routing, and metaharness.
5. Feed bounded evidence and Charon overlap notes to capability analysis, rather than marking every differing tool name as new.
6. Record commit, path, evidence class (source/documentation/benchmark), and feature gating. Deduplicate shared Pi concepts without discarding OMP-specific deltas.
7. Add fixture tests for helper exclusion, out-of-directory tools, absent descriptions, registration drift, duplicate names, and preservation of other repos' findings.

## Suggested first experiment

After scanner support, compare Charon baseline versus a tool-bridge/typed-result prototype and an LSP diagnostics prototype on matched tasks. Hold model, initial repo state, budgets, and acceptance tests fixed. Measure task success, malformed results, edit retries, tokens, wall time, and failure recovery. Use OMP's evaluation organization as inspiration without adopting its machine-specific benchmark infrastructure.

**Bottom line:** track OMP as a distinct source; prioritize execution and semantic coding improvements; keep Charon's persistent orchestration model; benchmark before replacing established contracts.
