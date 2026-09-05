# `charon.workspace` — build contract (Workspace RFC Increment 0 + overseer)

> **Status 2026-09-04: BUILT** (branch `overseer-primitive`). Phases C0–C4 of
> `~/Projects/acheron/docs/overseer-charon-primitive-plan.md`: workspace package (36 tests),
> policy (10), transports (9), overseer tools (16), MCP server (11), overseer cycle (12), role
> + skill (2); 1303 tests green in the full suite. Cross-kernel interchange with Acheron's JS
> kernel is proven in both directions (see the plan's checklist). Sections below marked
> "BUILT" describe what shipped; the rest is the original contract.

Design: `~/Projects/acheron/docs/overseer-charon-primitive-plan.md` (phases C0–C5) and
`docs/proposals/charon-workspace.md` (the RFC). Reference implementation to mirror:
Acheron's `src/overseer/kernel.js` (JS, schema-exact, 26 tests) — the Python package must be
**interchangeable** with it: same record shapes, same transition tables, same hash-chain
formula, and the checked-in Acheron bundle (`examples/workspaces/acheron-overseer.json`) must
import, verify, and re-export byte-for-byte in meaning.

```
src/charon/workspace/
  __init__.py      re-exports WorkspaceStore + errors + helpers
  records.py       canonical_json, sha256_hex, ids, per-type defaults/whitelists (schema-driven)
  machines.py      MachineSpecs on charon.orchestration.fsm (work_item, task, lease, knowledge_item, proposal, gate)
  store.py         WorkspaceStore: RFC storage layout, revision CAS, hash-chained events, artifacts, leases, gates
  bundle.py        export/import/validate (jsonschema when available; docs/contracts/workspace.schema.json)
  projections.py   projection() dict + PROJECT_STATUS.md / PLAN.md writers (Charon overseer-design layout)
  policy.py        evaluate_send_policy / evaluate_spawn_policy / redact  (port of Acheron policy.js)
  transport.py     SessionTransport ABC + LocalTmuxTransport + CharondTransport + FleetTransport  (C2)
  charond_client.py minimal JSON-lines client for ~/.charon/charond.sock (hello/list/attach/input/detach)  (C2)
  cli.py           python -m charon.workspace {export,import,validate,status,verify} --root <dir>
src/charon/tools/overseer_tool.py   the 20 tools (C2) — docs/contracts/overseer-tools.json is the table
src/charon/mcp_server.py            python -m charon.mcp_server — Charon tools over MCP stdio (C3)
tests/test_workspace_*.py, tests/test_tools_overseer.py, tests/test_mcp_server.py
```

Run tests from the worktree root with `./.venv/bin/python -m pytest tests/test_workspace_*.py -q`
(`pyproject` puts `src` on `pythonpath`; the venv is Python 3.13 with `jsonschema` 4.26 installed).
Never touch `~/.charon` or `~/.charon_state`; tests use `tmp_path`.

## Storage layout (identical to Acheron's overseer dir, RFC §Storage)

```
<root>/
  workspace.json               schema `workspace` record; extensions.acheron = {kernel_version, fencing_counters, <dispatcher keys>}
  records/<type>.json          {"<id>": record} for: work_item run task lease session artifact context_manifest policy knowledge_item replica proposal gate
  events/<replica_id>.jsonl    one schema `event` record per line, hash-chained
  artifacts/sha256/<hex>       raw bytes
  manifests/<id>.json          immutable context manifest copies
  exports/current.bundle.json  schema-valid bundle (write_projections())
  projection.json              presentation-neutral projection (write_projections())
  PROJECT_STATUS.md, PLAN.md   generated (write_projections(status=True))
```

`proposal` and `gate` are extension records (not in the schema): they persist under `records/`
like the others and export under `extensions.acheron.proposals[]` / `.gates[]`.

## Public API (`store.py`) — BUILT (36 tests: `tests/test_workspace_{store,bundle,policy}.py`); this section describes what shipped

Deviations from the first draft, all additive:
- `ws.events` holds every event known to the directory (all replicas, load order); `ws.own_events` is this
  replica's chain and `ws.seq`/`ws.last_digest` follow it. A second replica opening the same directory starts
  `events/<its id>.jsonl` and can `verify_chain(replica_id=…)` any chain. When a directory was written by Acheron
  (`events/acheron.jsonl`), the file that contains this replica's events is adopted as the own chain file.
- `ws.transition(..., event_id=…)` is replay-safe at the fsm level: a repeat with the same record, event, reason and
  base revision returns the record unchanged even when `expected_revision` is stale; a different transition under a
  used id raises `EventCollision`. `append_event` keeps the JS content-hash rule (event_type + subject_refs + payload).
- Persistence is write-through per public call (nested calls batch into one save); there is no `flush()`.
  `write_projections(status=True)` writes `exports/current.bundle.json`, `projection.json`, `PROJECT_STATUS.md`, `PLAN.md`.
- `lease_acquire` returns the list of leases (not `{leases}`); `lease_heartbeat` bumps revision without an event.
- Package-level helpers beyond the store: `import_bundle(root, bundle, replica_id=None)`, `read_bundle(path)`,
  `validate_bundle(bundle) -> [errors]` (jsonschema Draft 2020-12 + FormatChecker; structural-only fallback),
  `write_bundle_files`, `records_equal(a, b) -> [diffs]`, `build_projection`, `render_status_md`, `render_plan_md`,
  `summarize_work`, `evaluate_send_policy(**facts)`, `evaluate_spawn_policy(policy=, session_count=, max_blocks=)`,
  `looks_like_approval`, `redact`, `event_hash(event)`, `canonical_json`, `sha256_hex`, `iso_from_ms`.
- `machines.SPECS[type]` are real `MachineSpec`s (guards return `'GUARD:…'` reasons; reducers apply terminal
  timestamps / decision attribution from the dispatch payload `{'reason','actor','ts'}`); `store.transition` maps
  `fsm.TransitionRejected` → `IllegalTransition`/`GuardFailed(unmet=[criterion ids])`, `fsm.RevisionConflict` → `RevisionConflict`.
- Cross-language proof: `examples/workspaces/acheron-overseer.json` (JS-produced, 31 events) imports, `verify_chain()`
  is ok/31, the export validates with zero errors, re-import is record-identical, and appending from Python
  continues the JS chain (`previous_digest` links to the JS event).

### Original contract (kept for reference)

```python
from charon.workspace import (WorkspaceStore, RevisionConflict, IllegalTransition, GuardFailed,
                              LeaseConflict, EventCollision, NotFound, ValidationError)

ws = WorkspaceStore.open(root, workspace_id='workspace.acheron.<id>', slug='e2e', title='E2E',
                         roots=[{'kind': 'directory', 'locator': '/path'}], replica_id='replica.charon.<host>',
                         now=None)              # bootstraps workspace + replica records on first open
ws.workspace, ws.workspace_id, ws.replica_id, ws.seq, ws.events   # events = list of event records (this replica's chain)
ws.get(type, id) -> dict | None ; ws.list(type, pred=None) -> list[dict]
ws.create(type, fields, *, actor=None, event_id=None, provenance=None) -> dict      # fills recordBase; unknown fields → extensions
ws.update(type, id, expected_revision, patch, *, actor=None, event_id=None) -> dict  # CAS; 'status' rejected for FSM types; criteria merged by id
ws.transition(type, id, expected_revision, event, *, actor=None, reason=None, event_id=None, patch=None) -> dict
ws.attach_evidence(work_item_id, expected_revision, criterion_id, *, status='passed', artifact_ids=(), task_id=None, waiver_reason=None, actor=None)
ws.set_session_status(id, status, *, actor=None, extensions=None)                  # not an FSM; event only on change
ws.append_event(event_type, *, actor=None, subject_refs=(), payload=None, correlation_id=None, causation_id=None, base_revision=None, event_id=None, occurred_at=None) -> dict
ws.put_artifact(*, text=None, data=None, kind='document', title=None, media_type='text/plain', produced_by=None, task_id=None, run_id=None, session_id=None, metadata=None, source_revision=None) -> dict   # idempotent on digest
ws.read_artifact(id) -> str | None
ws.lease_acquire(*, task_id, session_id=None, scopes, mode='exclusive', ttl_ms=1_800_000, actor=None) -> list[dict]   # raises LeaseConflict(.conflicts=[{scope, lease}]) and records a `lease.conflict` event itself
ws.find_lease_conflicts(scope, mode='exclusive', exclude_task_id=None) -> list[dict]
ws.lease_heartbeat(id, ttl_ms=None) ; ws.lease_release(id, reason=None, *, actor=None) ; ws.expire_leases(*, actor=None) -> list[dict]
ws.create_gate(*, kind='question', question, options=(), refs=(), related=None, actor=None) ; ws.decide_gate(id, decision, actor=None)
ws.create_proposal(*, kind, title, rationale, alternatives=(), action=None, refs=(), actor=None) ; ws.transition_proposal(id, event, actor=None, reason=None)
ws.set_extension(key, value) ; ws.get_extension(key)                               # workspace.extensions.acheron.<key>
ws.export_bundle() -> dict ; WorkspaceStore.import_bundle(root, bundle, *, replica_id=None) -> WorkspaceStore
ws.projection() -> dict ; ws.write_projections(status=True) -> None
ws.verify_chain() -> {'ok': bool, 'count': int, 'broken_at': int | None, 'reason': str | None}
```

Persistence is write-through: every mutation atomically rewrites `records/<type>.json` (temp +
rename, use `charon.orchestration.fsm_store._atomic_write_json`) and appends its event line in
the same call. Reads come from memory (loaded on `open`).

### Rules (ADR-0003 / RFC invariants — same as the JS kernel)
- Every mutation = revision CAS + one event. Default actor `{'kind':'system','id':'system.charon'}`.
- `event_id` idempotency: same id + same content (event_type, subject_refs, payload) → return the
  existing event, no re-append; same id + different content → `EventCollision`.
- Terminal states absorb (`IllegalTransition`); unknown event → `IllegalTransition`.
- Work item `pass` guard: every `required` criterion is `passed` with ≥1 `evidence_artifact_ids`
  or `waived` with `waiver_reason`; else `GuardFailed(unmet=[criterion ids])`.
- Timestamps RFC3339 UTC with milliseconds and `Z` (`2026-09-03T21:30:01.000Z`) — match JS `toISOString()`.
- Ids match `^[A-Za-z][A-Za-z0-9_.:-]{0,127}$`. Generated ids: `work.<slug>.<10hex>`, `task.<10hex>`,
  `run.<10hex>`, `lease.<uuid>`, `manifest.<10hex>`, `artifact.sha256:<hex>`, `evt.<uuid>`, `gate.<uuid>`, `proposal.<uuid>`, `criterion.<8hex>`.

### Lifecycles (implement as `charon.orchestration.fsm.MachineSpec`s in `machines.py`; the schema enums are the states)

```
work_item:      backlog -ready-> ready -start-> active -submit-> review -pass-> done
                ready|active -block-> blocked -unblock-> active ; review -fail-> active ; non-terminal -cancel-> cancelled (reason → terminal_reason)
task:           pending -claim-> claimed -start-> running -wait-> waiting -resume-> running ; running -block-> blocked -unblock-> running
                claimed|running|waiting -succeed-> succeeded ; claimed|running|waiting|blocked -fail-> failed ; non-terminal -cancel-> cancelled (terminal sets completed_at; reason → result_summary)
lease:          requested -activate-> active ; active -release-> released | -expire-> expired | -revoke-> revoked (sets released_at, reason → release_reason)
knowledge_item: proposed -verify-> verified | -contest-> contested | -reject-> rejected | -retract-> retracted ; verified -contest/-retract/-supersede/-expire/-stale ; contested -verify/-reject/-retract ; stale -verify/-retract/-expire
proposal:       proposed -accept-> accepted -apply-> applied ; proposed -reject-> rejected ; proposed|accepted -withdraw-> withdrawn (decision sets decided_by/decided_at/decision_reason)
gate:           open -answer-> answered -resolve-> resolved ; open -resolve-> resolved (answer sets decided_by/decided_at, answer = {'answer': …} or the object passed)
session.status: starting|active|idle|disconnected|completed|failed|stopped via set_session_status (terminal-ish statuses set ended_at)
```

Map fsm errors: `fsm.RevisionConflict` → `RevisionConflict`, `fsm.TransitionRejected` → `IllegalTransition`
(or `GuardFailed` when a guard rejected with a reason string — put the unmet ids in the reason),
`fsm.EventCollision` → `EventCollision`. Use `fsm.dispatch` for the transition itself (event_id =
the event's id); the staged outbox event is what you append to the replica chain, enriched into the
schema `event` shape below. Persist the MachineInstance data as the record (revision = instance revision).

### Record defaults (`records.py`; whitelists per type from the schema, everything else → `extensions`)
- work_item: kind 'task', status 'backlog', priority 'normal', owner=actor, description '', dependency_ids/blocked_by_ids/assignees/run_ids [], scopes normalized `{kind:'path', selector, access:'write', recursive}`, acceptance_criteria normalized (id `criterion.<8hex>`, required True, verifier `{kind:'inspection', spec:{}}`, status 'pending', evidence_artifact_ids [])
- run: workflow_id 'workflow.acheron.dispatch', workflow_revision '1', status 'running', initiated_by=actor, started_at, event_cursors {replica_id: seq}, metrics {}; work_item_ids non-empty
- task: status 'claimed', owner=actor, attempt 1, max_attempts 3, scopes normalized, dependency_task_ids/lease_ids/evidence_artifact_ids [], started_at, idempotency_key = id; requires work_item_id, run_id, title, instruction
- lease: via lease_acquire — mode, status 'active', acquired_at/heartbeat_at, expires_at, fencing_token monotonic per resource.selector (counters in workspace.extensions.acheron.fencing_counters), owner_task_id, owner_session_id
- session: kind 'interactive', status 'starting', actor=actor, task_ids [], started_at, event_cursors {}, replica_id
- artifact: id `artifact.sha256:<hex>`, locator `workspace/artifacts/sha256/<hex>`, digest {algorithm:'sha256', value}, size_bytes, immutable True, status 'available', kind, media_type, produced_by
- context_manifest: compiler {name:'charon-overseer', version:'1'}, workspace_revision, event_cursors, built_at, query {}, budget {unit:'byte', limit:0, used:0}, policy_ids/entries/exclusions [], digest = sha256(canonical_json(entries)) unless given; immutable copy in `manifests/<id>.json`
- policy: version '1', status 'active', priority 100, effect 'allow', subject_selectors ['*'], conditions {}, rationale=name; requires name + non-empty actions
- knowledge_item: kind 'note', status 'proposed', confidence 0.5, review {required: True, status: 'pending'}, scopes/evidence_artifact_ids/tags [], body=title
- replica (bootstrap): kind 'local', trust 'trusted', identity `charon:<replica_id>`, cursors {}, capabilities [], policy_ids []
- proposal: status 'proposed', alternatives/refs [], proposed_by ; gate: status 'open', options/refs [], opened_by, answer None
- event: record_type 'event', id, workspace_id, revision 1, created_at, updated_at, replica_id, replica_sequence (1-based), logical_time (= sequence), event_type, occurred_at, actor, subject_refs [{record_type, id, revision?}], correlation_id?, causation_event_id? (from causation_id), base_revision?, payload, previous_digest (digest object of the previous event or None), digest

### Hash chain (must reproduce Acheron's digests bit for bit)
`digest.value = sha256_hex((previous_digest.value if previous_digest else '') + canonical_json(event_without_digest))`
`canonical_json(x) = json.dumps(x, sort_keys=True, separators=(',', ':'), ensure_ascii=False)` — this equals JS
`JSON.stringify` with sorted keys for the value types we use (ints stay ints, floats print the same, no NaN).
Test: import `examples/workspaces/acheron-overseer.json`, `verify_chain()` → ok with count 31.

### Projection (`projections.py`) — same shape as the JS kernel
`{generated_at, workspace{id,slug,title,roots,revision}, sessions[] (extensions spread to top level), work_items (tree with children[], criteria[{id,statement,required,verifier_kind,verifier_spec,status,evidence_count,evidence_artifact_ids,waiver_reason}], progress{required,met,total,percent}, tasks[]), runs, tasks, leases (live flag), gates (open first), proposals, decisions, recent_events (last 50, payload_head ≤200), counts}`
`PROJECT_STATUS.md` sections: Summary, Staffing, Work Division, Goals (checkbox tree with criteria marks ✓✗~·), Velocity, Risks — see Acheron `src/overseer.js:writeProjections` for the exact text.

## Overseer tools (`tools/overseer_tool.py`, C2) — table: `docs/contracts/overseer-tools.json`

Each entry's `name` (`acheron_*`) is the MCP name every surface exposes; `charon_name` is the
`TOOL_DEF['name']` registered in `charon.tools` (`WorkCreate`, `WorkDispatch`, …). `input_schema` = the
contract's `inputSchema`. Executors: `execute_overseer(name, params, ctx)` resolving the workspace
root from `ctx.metadata['workspace_root']` (or `ctx.state_dir / 'projects' / <slug> / 'workspace'`),
the actor from `ctx.agent_id` (`{'kind':'agent','id':'agent.<id>'}`), and a `SessionTransport` from
`ctx.metadata['transport']` (default: `AutoTransport` = charond if its socket exists, else local tmux).
Semantics per tool = Acheron's `src/overseer.js` HANDLERS (dispatch = task + manifest + leases + send +
`agent_intervention` event; checkpoint never completes an item; evidence for `approval` verifiers opens a
gate; spawn/propose/decide as there). Policies from `policy.py` before every send.

`SessionTransport` (transport.py):
```python
class SessionTransport(Protocol):
    def list_sessions(self) -> list[dict]          # {id, callsign, title, kind, agent, status, cwd, host_ref, wired}
    def read(self, session_id, mode='last_message', lines=2000) -> str
    def send(self, session_id, text, *, enter=True) -> str   # returns 'charond' | 'tmux' | 'fleet'
    def interrupt(self, session_id, key='ctrl-c') -> None
    def foreground(self, session_id) -> str | None  # process name; digests are held when it is a shell
```
`LocalTmuxTransport(socket='acheron')` uses `tmux -L <socket> ls -F '#{session_name}|#{@acheron_block}|#{@acheron_title}|#{@acheron_agent}|#{session_activity}'`, `send-keys -l`, `capture-pane -p -J`, `display -p '#{pane_current_command}'`. `CharondTransport(sock=~/.charon/charond.sock)` uses charond_client (`hello` → `list` → `input`/`attach replay` for reads). `FleetTransport` wraps `FleetSend` + `fleet.tmux_capture`.

## MCP server (`mcp_server.py`, C3)
`python -m charon.mcp_server --project-root <dir> [--state-dir <dir>] [--workspace-root <dir>] [--agent-id AG-…] [--profile overseer|all] [--tools A,B] [--approve none|all]`
- MCP stdio transport: one JSON-RPC 2.0 message per line on stdin/stdout (no headers); stderr for logs.
- Methods: `initialize` (echo protocolVersion, capabilities `{tools:{}}`, serverInfo `{name:'charon', version}`), `notifications/initialized` (no reply), `ping`, `tools/list`, `tools/call` → `charon.tools.execute_tool(name, arguments, ctx)` → `{content:[{type:'text', text}], isError}`.
- `--profile overseer`: expose exactly the 20 contract tools under their contract `name` (`acheron_*`) routed to `charon_name` executors, so the same overseer CLAUDE.md works on both surfaces; `--profile all`: every `ALL_TOOL_DEFS` (+dynamic) tool under its Charon name (`input_schema` → `inputSchema`).
- Approval: install `set_approval_callback` that auto-answers per `--approve` (default `none` = deny anything that asks).
- Tests: spawn the server as a subprocess, send initialize/tools/list/tools/call lines, assert framing and that a `tools/call` of `Read` on a tmp file returns its text.
- BUILT (`src/charon/mcp_server.py`, `tests/test_mcp_server.py` — 11 tests, `docs/mcp.md`). As specified, plus: `CHARON_OVERSEER_CONTRACT=<path>` overrides the contract file; every `notifications/*` method is silent; a missing overseer executor answers `isError` "tool not available on this Charon: <charon_name> (charon.tools.overseer_tool is not installed)"; `--approve none` answers approval requests through `set_approval_callback` + `respond_to_approval(id, False)` so gated calls return `Blocked: … (user denied)` immediately (verified: `rm -rf` via Bash returns in <1 s); dynamic plugin tools are included in `--profile all` via `dynamic_loader.get_all_tool_defs`.

## Cycle (`cycle.py`, C4) — BUILT

`collect_events(store, since, transport=None) -> (lines, cursor, session_status)` turns workspace events
newer than a per-replica cursor (`record.transitioned` on work items / terminal task states / expired
leases, `gate.decided`, `lease.conflict`, `session.status`, `session.spawned`) and, with a transport,
observed session-status changes (running→idle = "finished" with the redacted last-message head,
waiting, error, detached) into Acheron-worded digest lines. `build_digest(store, cycle, name, lines)`
renders exactly Acheron's `digestMessage`. `run_cycle(store, deliver=..., transport=None, cadence=None,
overseer_session_id=None, force=False)` applies Acheron's hold rules (nothing new, shell in the
overseer pane's foreground, overseer running/starting, user typing <15 s when the transport reports
`user_typed_ago_ms`, hourly cap from `cadence.max_cycles_per_hour`), delivers, and only then records
`cycle.started` and persists the workspace extensions `cycle_count`, `cycle_cursor`, `cycle_stamps`,
`cycle_session_status` — a failed delivery keeps the events. Delivery strategies:
`deliver_to_session(transport, session_id)` and `deliver_to_agent(state_dir, owner_agent_id)`
(enqueues an `agent_task` whose instruction is the digest + "Follow the overseer skill.").

Loop: task type `overseer_cycle` in `charon_loop.process_task` with fields `workspace_root`,
`delivery {kind: 'agent'|'session', owner_agent_id?, session_id?}`, `cadence {max_cycles_per_hour}`,
optional `transport {kind: 'none'|'auto'|'tmux'|'charond', socket?, sock_path?}`, `workspace_name`,
`replica_id` (default `replica.charon.loop`), `force`. Held cycles succeed with summary `held: <reason>`;
run.log gets `overseer_cycle_delivered` / `overseer_cycle_held`. Recurrence uses the existing
`interval_minutes` path (the recurring copy now carries the cycle fields).
`conversation_runtime.enqueue_overseer_cycle(state_dir, workspace_root=…, delivery=…, interval_minutes=10, cadence=None, transport=None, workspace_name=None, replica_id=None)` schedules one.


## Overseer tools + transports — BUILT (`tests/test_tools_overseer.py` 16 tests, `tests/test_workspace_transport.py` 9 tests)

- `src/charon/tools/overseer_tool.py`: the 20 contract tools registered as `OverseerFleet OverseerRead WorkCreate WorkUpdate
  WorkTransition WorkList WorkDispatch OverseerIntervene OverseerInterrupt WorkCheckpoint WorkEvidence LeaseHeartbeat LeaseRelease
  OverseerSpawn OverseerPropose OverseerDecide OverseerLabel OverseerReport OverseerAsk OverseerWait` (`TOOL_DEF` = contract
  `charon_name`/`description`/`inputSchema`; guarded import in `charon.tools` like the fleet tools). `execute_overseer(mcp_name, params, ctx)`
  is the single executor; `HANDLERS` are keyed by the MCP (`acheron_*`) names, which is what `charon.mcp_server --profile overseer` routes.
- Context: `metadata['workspace_root']` (else `<state_dir>/projects/<slug>/workspace`; an existing `workspace.json` keeps its id —
  Acheron-written dirs open unchanged), `metadata['transport']` (else `AutoTransport()`), `metadata['policy']` =
  `{paused, forward_approvals, spawn: allow|confirm|deny, max_blocks, wired: 'all' | [session ids/callsigns/block ids],
  user_typed_ago_ms: {session id: ms}}` (default `wired: 'all'` — there is no wire UI in Charon; Acheron passes its wires),
  actor `agent.<ctx.agent_id>`. Stores are cached per root (`reset_store_cache()` for tests).
- Results: `ToolResult(content=json, details=result)`; errors are `is_error` with Acheron's phrasing (`policy.<code>: …`, `CONFLICT: …`,
  `rate limit: 60 mutating calls in 10 minutes …`, `busy` is `policy.busy: …`). Every call is journaled to `<root>/journal.jsonl`
  (`{ts, name, args, ok, ms, surface, error?}`). `OverseerReport` writes `<root>/overseer.json` + `write_projections(status=True)` and
  keeps `cycle_count`/`report` in workspace extensions.
- Deviations from Acheron's dispatcher: `spawn` delegates to `transport.spawn()` (charond can spawn; tmux/fleet cannot → is_error
  explaining that spawning needs Acheron or charond); `label` calls `transport.label()` when available (tmux: `set-option @acheron_title`);
  `wait` re-opens the store from disk every 0.5 s and returns events appended by other processes; the digest/cadence engine is not here
  (that is the loop's `overseer_cycle`, phase C4).
- `src/charon/workspace/transport.py`: `SessionTransport` protocol + `LocalTmuxTransport(socket=$CHARON_TMUX_SOCKET|'acheron')`
  (Acheron's `@acheron_*` tags via `list-sessions -F`, `capture-pane -p -J`, single-line `send-keys -l --`, multi-line
  `set-buffer` + `paste-buffer -p` (bracketed paste), `display -p '#{pane_current_command}'` for the shell guard, `set-option @acheron_title`
  for labels; session/callsign/block-id/tmux-name all resolve), `CharondTransport` over `charond_client.py` (JSON lines:
  hello/list/input/attach+replay→snapshot/detach/spawn/ping; states idle/working/blocked/done/exited → idle/running/waiting/idle/disconnected;
  multi-line sends wrapped in bracketed paste; `foreground()` is None), `FleetTransport` (thin over `charon.fleet.tmux_capture`, no spawn),
  `CompositeTransport` (routes by who listed the session), `AutoTransport()` (charond when `$CHARON_SOCK`/`~/.charon/charond.sock` exists,
  merged with local tmux; else tmux only). `extract_last_message()`/`clean_capture()` are ports of Acheron's extractor (+ braille spinner
  and terminal-query residue stripping).
- Test isolation: transport tests use throwaway tmux/charond sockets under `/tmp/ct-*` (`SUN_LEN` — pytest's tmp_path is too deep) and
  a private `-L charon-test-*` socket; the MCP smoke sets `CHARON_TMUX_SOCKET`/`CHARON_SOCK` so nothing touches the user's fleet.

## System map (`skills/system-map/`, phase M2 of Acheron's `docs/system-map-plan.md`) — BUILT

`skills/system-map/{mapcore,validate,extract,query}.py` (stdlib only) are a parity port of Acheron's
`src/overseer/map/*.js`: same declared-map schema (`docs/contracts/system-map.schema.json`, vendored
byte-identical), same validation rules (overlap = error, unmapped = gap, anchors, cycles as warnings),
same language adapters (JS/TS, Rust, Python, Swift), same derived-facts JSON. `tests/test_system_map.py`
runs both implementations on Acheron's fixture repo and asserts identical results.
- Tool: `SystemMap` (`action: validate|extract|query`, `root`, `map`, `component`, `out`) in
  `src/charon/tools/system_map_tool.py`.
- Skill record + installer: `src/charon/workspace/system_map_skill.py` (`skill_record()`,
  `install_system_map_skill(state_dir)` → `.charon_state/skills/system-map/SKILL.md`).
- Acheron prefers this skill when `$CHARON_SKILLS_DIR`/`~/Projects/charon/skills/system-map` exists and
  falls back to its JS extractor otherwise; the overseer's `acheron_map_*` tools (Acheron side) call
  whichever is present.
