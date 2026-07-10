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
