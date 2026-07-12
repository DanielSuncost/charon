# charon (Python agent runtime)

The Python agent runtime: the installable `charon` package, organized into
subpackages that implement Charon's daemon loop, conversations, memory, and
orchestration.

## Running

```bash
cd charon
python src/charon/charon_loop.py      # by path, or:
PYTHONPATH=src python -m charon.charon_loop
```

Stop it by touching the stop file (`./CHARON_STOP` by default, or set
`CHARON_STOP_FILE`). State and logs are written to `.charon_state/`
(override with `CHARON_STATE_DIR` / `--state-dir`).

## Entry point: `charon_loop.py`

The persistent daemon. It owns the task queue, runs a heartbeat cycle,
recovers stuck tasks, and drives shade delegation, boundary coordination
(overlap detection, proposals, resolutions), and agent spawning. Inside
the loop it also starts/ticks the background subsystems: `fleet_sync`,
`consolidation`, `judge_loop_driver`, and `autonomous` self-assignment.

## Subsystem map

| Cluster | Modules | What it does |
|---|---|---|
| Orchestration / daemon core (`agents/`) | `charon_loop`, `agent_runtime`, `agent_lifecycle`, `agent_policy`, `autonomous`, `goal_runtime`, `task_ledger`, `boundary_runtime`, `intervention_graph`, `inter_agent_rooms`, `threads`, `session_registry` | Task queue, agent lifecycle and policy, autonomous goal work, boundary coordination, multi-agent rooms, cross-agent decision threads, session tracking |
| Conversation (`conversation/`) | `conversation_engine`, `conversation_runtime`, `conversation_store`, `conversation_participants`, `conversation_index` | `conversation_engine` is the multi-turn LLM loop with tool use; `conversation_runtime` manages the queue and participants; `conversation_store` persists turns as JSONL |
| Context / prompt (`context/`) | `context_store`, `context_compactor`, `context_assembler`, `context_transfer`, `system_prompt_builder` | Working-context storage, compaction, assembly into prompts, cross-provider context transfer |
| Memory (`memory/`) | `memory_engine`, `episodic`, `procedural`, `execution_memory`, `consolidation`, `assimilation`, `memory_extractor`, `memory_indexer`, `memory_bridge`, `embedding_client`, `embedding_worker`, `user_model_structured` | `memory_engine` is the hub: hybrid vector (sqlite-vec) + FTS5 search. Episodic/procedural/execution tiers, consolidation and assimilation passes, extraction/indexing, embeddings via an `embedding_worker` subprocess, structured user model |
| Libris (`libris/`) | `libris_orchestrator`, `libris_agents`, `libris_specialists`, `libris_runtime`, `libris_refinement`, `libris_convergence`, `libris_report`, `libris_procurement_ingest` | Multi-agent research: coordinator, researchers, judge critique, convergence, cited HTML report generation |
| Judge (`judge/`) | `judge_engine`, `judge_loop_driver` | Iterative optimization loops (snapshot → implement → judge → keep/rollback); the driver ticks active loops from the daemon |
| Devop (`devop/`) | `devop_runtime`, `devop_agents`, `devop_projection` | Autonomous software-development operations |
| Shade (`shade/`) | `shade_orchestrator`, `shade_stats` | Ephemeral scoped worker agents: sequential contracts, parallel phases, stats |
| Fleet / remote (`fleet/`) | `fleet_sync`, `fleet_registry`, `fleet_memory`, `harbor`, `remote_onboard`, `external_session_launcher`, `tmux_capture` | Remote task dispatch (Harbor protocol), fleet registry/sync/memory, external and tmux session integration |
| Automation / batch (`automation/`) | `automation_runtime`, `automation_scheduler`, `batch_orchestrator`, `checkpoint_manager` | Scheduled automations, parallel shade batches, shadow-git checkpoints |
| Providers / model / auth (`providers/`) | `providers/` (anthropic, openai-compat, httpx backends), `provider_bridge`, `worker_provider`, `model_registry`, `llm_adapter`, `charon_auth`, `oauth_lock` | Raw-httpx provider clients, provider switching, model registry, auth and OAuth locking |
| Persistence (`infra/`) | `store_adapter`, `store`, `project_registry`, `project_registry_loader`, `diagnostics`, `tool_approval`, `orchestration_trace` | `store_adapter` is the single SQLite entry point, backed by `infra/store.py` (WAL). Project registry (`project_registry_loader` is a vestigial re-export shim), diagnostics, tool approval, orchestration trace/span substrate |
| Tools (`tools/`) | `tools/` (21 tool modules) | Built-in agent tools exposed via `ALL_TOOL_DEFS` / `execute_tool`, plus a dynamic plugin loader (`tools/dynamic_loader.py`) |

## Dependency hubs

Two modules are imported by roughly 30 others each and are the places to
look first when tracing behavior:

- **`store_adapter`** — all SQLite persistence goes through here.
- **`memory_engine`** — all memory search/indexing goes through here.

## Import convention

All imports are absolute package imports (`from charon.memory.memory_engine
import ...`). The package lives under `src/` and is importable either via
`pip install -e .`, via `PYTHONPATH=src`, or through the pytest `pythonpath`
setting. Entry modules that are launched by file path (`charon_loop.py`,
`memory/embedding_worker.py`) carry a small `__package__` guard that puts
`src/` on `sys.path` before importing `charon.*`.

## Subpackage layout

`agents/`, `conversation/`, `context/`, `memory/`, `libris/`, `judge/`,
`devop/`, `shade/`, `fleet/`, `automation/`, `providers/`, `tools/`,
`infra/` — module filenames are unchanged from the flat layout; they are
only grouped into the clusters above. `charon_loop.py` and `charon_gym.py`
stay at the package root.

## Error handling policy

Charon is a personal daemon: graceful degradation is deliberate. A failed
subsystem (memory, a provider, an optional tool) must degrade the experience,
never crash the loop. That policy has hard rules:

- **Every silent fallback records to diagnostics.** Any `except` that swallows
  an error and continues must call `charon.infra.diagnostics.record` (the
  guarded `_diag` pattern) so degradation is observable after the fact.
- **State files are written atomically** (temp file + `os.replace`; see
  `charon.infra.fileio.write_json_atomic`) so a crash can never leave a
  half-written file.
- **Unreadable state is preserved, never overwritten.** If an existing state
  file cannot be parsed, it is quarantined as `<name>.corrupt-<n>`
  (`charon.infra.fileio.read_json_or_quarantine`) before the caller falls back
  to an empty default — a transient read error must never become permanent
  data loss on the next write. The same principle applies to the SQLite user
  model: destructive rewrites run in a single transaction and roll back to the
  prior state on failure.
- **Auth fails closed.** An auth artifact that cannot be validated (e.g. an
  unparseable JWT) is treated as expired/invalid and triggers the recovery
  path (refresh), rather than being optimistically accepted and failing later.
- **Background work must reach a terminal state.** A crashed worker marks its
  batch/loop failed so status queries terminate instead of reporting
  'running' forever.

## Configuration

All `CHARON_*` environment variables are defined as typed accessors in
`charon/infra/config.py` (one function per variable, read at call time —
never cached at import). Product code must go through those accessors
instead of `os.environ`.

| Variable | Default | Effect |
|---|---|---|
| `CHARON_STATE_DIR` | unset | State directory override. Fallbacks differ by consumer: `charon_loop` uses `./.charon_state`, diagnostics and provider_bridge use `~/.charon_state`. |
| `CHARON_STOP_FILE` | `./CHARON_STOP` | Sentinel file whose existence stops the loop. |
| `CHARON_NO_SQLITE` | `0` | `1` disables the SQLite store; JSON-file persistence is used instead. |
| `CHARON_STDOUT_EVENTS` | `1` | `1` mirrors loop events to stdout as JSONL; tests set `0`. |
| `CHARON_DEBUG_TRACE` | `0` | `1` enables the high-volume JSONL trace in `<state-dir>/debug.log`. |
| `CHARON_LOOP_SLEEP` | `2.0` | Seconds slept between loop cycles. |
| `CHARON_MAX_CYCLES` | `0` | Stop after N cycles; `0` = run forever. |
| `CHARON_MAX_CONSEC_FAIL` | `5` | Consecutive cycle failures before the loop aborts. |
| `CHARON_STALE_IN_PROGRESS_SEC` | `60` | Seconds before a heartbeat-less in-progress task is requeued. |
| `CHARON_HEARTBEAT_INTERVAL` | `30` | Loop cycles between heartbeat events. |
| `CHARON_REQUIRE_TMUX` | `1` | `1`: newly created agents require a tmux session. |
| `CHARON_SHADE_REQUIRE_TMUX` | `0` | `1`: shade contract agents require tmux (opt-in). |
| `CHARON_AGENT_PLANNER` | unset | `heuristic` or `llm` forces the agent planner mode. |
| `CHARON_AGENT_SHELL_TIMEOUT` | `45` | Timeout (s) for shell actions run by agent runtimes. |
| `CHARON_SPEC_WINDOW` | `10` | Recent task summaries considered for soft specialization. |
| `CHARON_SPEC_MIN_TASKS` | `3` | Minimum tasks before a specialization label is generated. |
| `CHARON_SPEC_INTERVAL` | `300` | Seconds between specialization refreshes. |
| `CHARON_AUTONOMOUS` | unset | `1/true/on` forces autonomous mode on; `0/false/off` forces it off; otherwise the config file decides. |
| `CHARON_SKIP_APPROVAL` | `0` | `1/true/yes` disables all tool approval checks. |
| `CHARON_BROWSER_HEADLESS` | `1` | Legacy, inverted: `0` shows the browser; anything else hides it. In `browser_settings` resolution the empty string means "no opinion". |
| `CHARON_X_PROFILE_DIR` | unset | Chromium profile dir override for the X tool (default `<state-dir>/browser/x`). |
| `CHARON_SEARXNG_URL` | unset | Base URL of a self-hosted SearXNG instance for web search. |
| `CHARON_EMBED_MODEL` | `BAAI/bge-base-en-v1.5` | sentence-transformers model for memory embeddings. |
| `CHARON_EMBED_BACKEND` | `worker` | `worker` uses the embedding subprocess; `local` loads the model in-process (tests). |
| `CHARON_EMBED_DEVICE` | unset | Torch device for embeddings (`cpu`, `cuda`, `mps`); unset = auto. |
| `CHARON_EMBED_IDLE_SECS` | `120` (floor 15) | Idle seconds before the embedding worker exits. |
| `CHARON_CONSOLIDATION_MODEL` | unset | Model tier override for memory consolidation. |
| `CHARON_CONSOLIDATION_INTERVAL` | unset | Heartbeats between consolidation scans. |
| `CHARON_CONSOLIDATION_ENABLED` | unset | Only the literal `false` disables consolidation (no force-enable value). |
| `CHARON_LOCAL_BASE_URL` | unset | OpenAI-compatible base URL for the `local` provider (falls back to `CHARON_LMSTUDIO_BASE_URL`, then `http://127.0.0.1:1234/v1`). |
| `CHARON_LMSTUDIO_BASE_URL` | unset | Legacy alias for the local base URL, honored as a fallback. |
| `CHARON_LOCAL_API_KEY` | `not-needed` | API key sent to the local provider endpoint. |
| `CHARON_LOCAL_MODEL` | unset | Model id override for the local provider (`lmstudio/` prefix stripped). |
| `CHARON_SHADE_MODEL_MODE` | unset | Overrides the model-selection mode for shade agents. |
| `CHARON_SHADE_MODEL` | unset | Pins shades to a fixed model (implies mode `fixed`). |
| `CHARON_PROVIDER` | unset | Provider requested at TUI launch (e.g. `local`, `claude-code`). |
| `CHARON_RESUME` | unset | Agent id (or `latest`) whose conversation the TUI resumes at launch. |
| `CHARON_AGENT` | unset | Agent id/name the TUI session binds to at launch. |
