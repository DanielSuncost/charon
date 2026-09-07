"""Charon persistent Python kernel worker — runs as a standalone child process.

Launched by pykernel_tool.py via `sys.executable <this file>`. Speaks a tiny
line-delimited JSON protocol over stdin/stdout:

    in:  {"cmd": "init", "project_root": "...", "state_dir": "...",
          "agent_id": "...", "src_dir": "...", "topology_depth": 0,
          "topology_budget": {...} or null}
    out: {"ok": true}

    in:  {"cmd": "run", "code": "...", "timeout_sec": 60}
    out: {"ok": true, "stdout": "...", "stderr": "...", "result": "<repr or omitted>"}
      or {"ok": false, "stdout": "...", "stderr": "...", "error": "...", "traceback": "..."}

    in:  {"cmd": "reset"} -> {"ok": true}          (clears the namespace)
    in:  {"cmd": "shutdown"} -> {"ok": true}, exits

Code runs against a single persistent `namespace` dict for the life of this
process — that dict IS the kernel's state. It outlives any one tool call, any
one conversation turn, and (unlike the model's context window) survives
context compaction entirely, because none of it lives in the transcript.

No sandboxing here, deliberately consistent with the rest of Charon's
execution tools (Bash, ExecuteCode): this process has the daemon's own OS
permissions.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import time
import traceback
import types


def _trace_rlm_node(state_dir, *, node_id, parent_id, root_task_id, objective,
                     depth, budget, status, output_ref) -> None:
    """Append one record to this tree's rlm-node trace log (best-effort).

    Shape matches docs/contracts/rlm-node.schema.json. One JSONL file per
    root_task_id under state_dir/rlm/ — a durable trace of every rlm() call
    for debugging/replay, satisfying the RLM design's "trace graph" goal
    without a new FSM: it just wraps the existing shade-contract lifecycle
    (output_ref is the shade's contract_id).
    """
    if not state_dir:
        return
    try:
        from pathlib import Path
        path = Path(state_dir) / 'rlm' / f'{root_task_id}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            'schema_version': '1.0',
            'id': node_id,
            'parent_id': parent_id,
            'root_task_id': root_task_id,
            'objective': objective,
            'depth': depth,
            'budget': budget,
            'usage': {},
            'status': status,
            'output_ref': output_ref,
        }
        with open(path, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception:
        pass  # tracing is best-effort observability, never load-bearing


def _maybe_promote_output(state_dir, *, objective, output, shade_id, contract_id) -> dict:
    """Judge whether a completed rlm() call's output is worth remembering,
    and write it into durable project memory if so.

    Never promotes on the shade's own say-so: an independent LLM judge
    (judge_engine.score_text, the same one-shot verdict format Refine and
    the aesthetic judge loop use) scores the output against a rubric first.
    Best-effort — any failure here (no provider, judge error, memory write
    failure) must never fail the rlm() call it's attached to; it just means
    nothing gets promoted.
    """
    if not output or not state_dir:
        return {'promoted': False, 'promotion_score': None}
    try:
        from charon.providers.model_registry import get_shade_provider_and_model
        from charon.judge.judge_engine import score_text

        provider, model, _meta = get_shade_provider_and_model(state_dir)
        rubric = (
            "You are deciding whether a sub-agent's output is worth remembering as "
            "durable project knowledge — not routine progress, not a trivial or "
            "obvious result, but a genuine, reusable finding (a real bug, a specific "
            "root cause, a concrete decision) that would help on a *future*, "
            "unrelated task.\n\n"
            "Score 0-10: 8-10 is a specific, reusable finding; 4-7 is real but narrow "
            "or already-obvious; 0-3 is routine status, vague, or not worth keeping."
        )
        verdict = score_text(output, rubric=rubric, context=objective, provider=provider, model=model)
        if verdict.error or verdict.score < 7.0:
            return {'promoted': False, 'promotion_score': verdict.score}

        from pathlib import Path
        from charon.memory.memory_engine import MemoryEngine
        engine = MemoryEngine(Path(state_dir))
        try:
            engine.add(
                output, category='rlm_finding', tier='project',
                source_agent=shade_id, source_conv=contract_id,
            )
        finally:
            engine.close()
        return {'promoted': True, 'promotion_score': verdict.score}
    except Exception:
        return {'promoted': False, 'promotion_score': None}


def _bridge_module(
    project_root: str, state_dir: str, agent_id: str,
    topology_depth: int = 0, topology_budget: dict | None = None,
) -> types.ModuleType:
    """Build the `charon` object exposed inside kernel globals.

    Deliberately thin: spawn_shade and rlm both re-enter Charon's own
    SpawnShade tool function rather than reimplementing shade-spawning, so
    kernel-issued spawns and normal tool-call spawns stay behind one
    implementation. rlm() additionally polls shade_orchestrator.get_contract
    (read-only) to turn that fire-and-forget spawn into a blocking call —
    see rlm()'s own docstring for why polling, not a synchronous
    reimplementation of the shade's phase loop, is the safe way to do that.

    topology_depth/topology_budget: inherited from whatever ToolContext
    first spawned this kernel (see pykernel_tool._KernelWorker) — without
    this, every kernel-issued spawn would silently root a brand-new tree at
    this kernel's own agent_id, even when this kernel itself belongs to a
    shade deep in someone else's tree.
    """
    mod = types.ModuleType('charon')
    # Mutated by main() right before each 'run' dispatch (see bottom of this
    # file), read by rlm()'s poll loop — the one thing that legitimately
    # changes per call rather than per kernel (unlike topology_depth/budget,
    # a call's timeout isn't fixed for the kernel's whole lifetime).
    call_state: dict = {'deadline': None}
    mod._call_state = call_state

    def spawn_shade(goal: str, scope=None, constraints=None, expected_outputs=None) -> dict:
        """Enqueue a shade agent and return immediately with a handle.

        Fire-and-forget — this never blocks waiting for the shade's answer.
        The shade runs in its own background thread in the daemon process;
        results surface later via the normal agent inbox / contract status,
        not as a return value here.
        """
        from pathlib import Path

        from charon.tools import ToolContext
        from charon.tools.shade_tool import execute_spawn_shade

        ctx = ToolContext(
            project_root=Path(project_root),
            agent_id=agent_id,
            state_dir=Path(state_dir) if state_dir else None,
            topology_depth=topology_depth,
            topology_budget=topology_budget,
        )
        result = execute_spawn_shade(
            {
                'goal': goal,
                'scope': list(scope or []),
                'constraints': list(constraints or []),
                'expected_outputs': list(expected_outputs or []),
            },
            ctx,
        )
        if result.is_error:
            raise RuntimeError(result.content)
        return dict(result.details or {})

    def rlm(objective: str, *, child_agent_id=None, peer_agent_id=None, scope=None,
            constraints=None, expected_outputs=None, contract_id=None, peer_task_id=None,
            retain: bool = False, promote: bool = False, task_complexity: str = 'normal',
            poll_interval: float = 0.5, max_peer_messages: int = 50) -> dict:
        """Call a sub-agent — or a peer agent — and block for its result,
        like a function call.

        Unlike spawn_shade (fire-and-forget), this waits for the target to
        finish and returns its actual output — the real recursive-call
        primitive: call a sub-agent (or message a peer) the way you'd call
        a function.

        PyKernel clamps a single `run` call to at most 300s. rlm() self-
        limits its own wait to a margin under whatever timeout_sec this
        PyKernel call was actually given, returning a clean
        {'status': 'still_running', ...} *before* that ceiling would cut it
        off — read the returned id (contract_id or peer_task_id) and pass it
        back in on a follow-up call to keep waiting on the same target
        instead of duplicating the work. The id is also printed to stdout
        immediately, in case a caller-supplied huge poll_interval or some
        other edge case still lets PyKernel's own SIGINT land first.

        A shade-contract wait can also come back {'status': 'stalled', ...}
        — distinct from 'still_running' — if the contract goes quiet for
        too long (config.shade_contract_stall_seconds(), default 300s) with
        no phase update: the worker process behind it is almost certainly
        dead. Unlike 'still_running', resuming a 'stalled' call with the
        same contract_id won't help; treat it like a failure.

        child_agent_id: call an existing retained (idle) shade instead of
        spawning a fresh one. Errors — never silently spawns a fresh shade
        under that id — if it isn't found, isn't a shade, or isn't idle.
        Mutually exclusive with peer_agent_id.

        peer_agent_id: message another already-running, non-shade agent and
        wait for its reply — not a spawn, so it isn't governed by
        TopologyBudget's depth/breadth accounting. Delivered via the real
        task queue (enqueue_agent_task), so it reaches that agent's actual
        ConversationEngine next time the daemon gives it a turn — genuinely
        different from the inert agent_inbox mechanism. Capped by
        max_peer_messages per (sender, peer) pair to prevent two agents
        messaging each other in an unbounded loop. Mutually exclusive with
        child_agent_id.

        retain: only meaningful on a fresh spawn (child_agent_id/peer_agent_id
        omitted). If true, the new shade goes idle instead of stopping once
        this call returns, and can be called again later via
        rlm(objective, child_agent_id=<the id from this call's result>).

        promote: if the call completes successfully, have an independent
        LLM judge score whether the output is a genuine, reusable finding
        worth remembering for this project (not routine or trivial) before
        writing it into durable project memory — never on the shade's own
        say-so. See the result's 'promoted'/'promotion_score' keys. Only
        applies to shade calls, not peer messages.

        task_complexity: 'simple'/'normal'/'complex' — only meaningful on a
        fresh spawn; 'complex' asks for the strong model tier. Downgraded
        automatically once this tree's token_budget is mostly spent,
        regardless of what's requested here.
        """
        import time as _time
        from pathlib import Path

        from charon.tools import ToolContext

        ctx = ToolContext(
            project_root=Path(project_root),
            agent_id=agent_id,
            state_dir=Path(state_dir) if state_dir else None,
            topology_depth=topology_depth,
            topology_budget=topology_budget,
        )

        fresh_targets = [t for t in (child_agent_id, peer_agent_id) if t]
        resume_handles = [h for h in (contract_id, peer_task_id) if h]
        if len(fresh_targets) > 1:
            raise ValueError('child_agent_id and peer_agent_id are mutually exclusive')
        if len(resume_handles) > 1:
            raise ValueError('contract_id and peer_task_id are mutually exclusive')
        if fresh_targets and resume_handles:
            raise ValueError('cannot combine a fresh target (child_agent_id/peer_agent_id) with a resume handle (contract_id/peer_task_id)')

        # Shared self-limiting margin — see the docstring above.
        margin_sec = max(3.0, poll_interval * 5)

        if peer_agent_id or peer_task_id:
            from charon.conversation.conversation_runtime import enqueue_agent_task
            from charon.infra.queue_io import load_queue_atomic

            if peer_task_id:
                task_id = peer_task_id
            else:
                from charon.agents.topology_budget import try_reserve_peer_message

                ok, reason = try_reserve_peer_message(
                    ctx.state_dir, sender_agent_id=agent_id, peer_agent_id=peer_agent_id,
                    max_messages=max_peer_messages,
                )
                if not ok:
                    raise RuntimeError(f'cannot message {peer_agent_id!r} — {reason}')
                task = enqueue_agent_task(
                    ctx.state_dir, owner_agent_id=peer_agent_id,
                    instruction=f'Message from agent {agent_id}: {objective}',
                )
                task_id = task['id']

            print(json.dumps({'rlm_peer_task_id': task_id}), flush=True)

            terminal = {'completed', 'failed'}
            while True:
                queue = load_queue_atomic(Path(ctx.state_dir) / 'queue.json') if ctx.state_dir else []
                task = next((t for t in queue if t.get('id') == task_id), None)
                if not task:
                    raise RuntimeError(f'peer task {task_id} not found')
                if task.get('status') in terminal:
                    break
                deadline = call_state.get('deadline')
                if deadline is not None and _time.time() > deadline - margin_sec:
                    return {'status': 'still_running', 'peer_task_id': task_id}
                _time.sleep(poll_interval)

            return {
                'status': task.get('status'),
                'output': task.get('result_summary'),
                'peer_task_id': task_id,
            }

        from charon.tools.shade_tool import execute_spawn_shade
        from charon.shade.shade_orchestrator import get_contract, is_contract_stale

        if contract_id:
            cid = contract_id
        elif child_agent_id:
            from charon.tools.shade_tool import execute_reactivate_shade
            from charon.agents.topology_budget import effective_budget

            budget = effective_budget(ctx)
            depth = int(getattr(ctx, 'topology_depth', 0) or 0) + 1
            try:
                handle = execute_reactivate_shade(
                    ctx.state_dir, child_agent_id, objective, ctx, depth=depth, budget=budget,
                )
            except KeyError:
                raise RuntimeError(f'{child_agent_id!r} has no retained lifecycle (never spawned with retain=True, or already stopped).') from None
            except Exception as e:
                raise RuntimeError(f'cannot reactivate {child_agent_id!r}: {e}') from e
            cid = handle['contract_id']
        else:
            result = execute_spawn_shade(
                {
                    'goal': objective,
                    'scope': list(scope or []),
                    'constraints': list(constraints or []),
                    'expected_outputs': list(expected_outputs or []),
                    'retain': bool(retain),
                    'task_complexity': str(task_complexity or 'normal'),
                },
                ctx,
            )
            if result.is_error:
                raise RuntimeError(result.content)
            details = dict(result.details or {})
            cid = details.get('contract_id')
            if not cid:
                raise RuntimeError('spawn succeeded but returned no contract_id')
            if retain:
                # The only way to learn the new shade's id for a later
                # reactivation — printed alongside the contract id below.
                print(json.dumps({'rlm_shade_id': details.get('shade_id')}), flush=True)

        # Printed before the wait begins — see the docstring above.
        print(json.dumps({'rlm_contract_id': cid}), flush=True)

        terminal = {'completed', 'failed'}
        while True:
            contract = get_contract(ctx.state_dir, cid)
            if not contract:
                raise RuntimeError(f'contract {cid} not found')
            if contract.get('status') in terminal:
                break
            if is_contract_stale(contract):
                # Gone quiet for over config.shade_contract_stall_seconds()
                # with no phase update — the worker process behind this
                # contract is almost certainly dead (hard-killed, orphaned).
                # Distinct from 'still_running' so a caller resuming via
                # contract_id doesn't keep blindly waiting on it forever.
                return {
                    'status': 'stalled', 'contract_id': cid, 'shade_id': contract.get('shade_agent_id'),
                    'reason': 'no phase progress for too long — likely an orphaned worker process; do not resume, treat as failed',
                }
            deadline = call_state.get('deadline')
            if deadline is not None and _time.time() > deadline - margin_sec:
                return {'status': 'still_running', 'contract_id': cid, 'shade_id': contract.get('shade_agent_id')}
            _time.sleep(poll_interval)

        phases = contract.get('phases') or []
        succeeded = contract.get('status') == 'completed'
        output = phases[-1].get('result_summary') if phases else None
        meta = contract.get('metadata') or {}
        root_task_id = (meta.get('topology_budget') or {}).get('root_id') or agent_id
        _trace_rlm_node(
            ctx.state_dir, node_id=cid, parent_id=agent_id, root_task_id=root_task_id,
            objective=objective, depth=meta.get('topology_depth'),
            budget=meta.get('topology_budget'), status=contract.get('status'), output_ref=cid,
        )
        result = {
            'status': contract.get('status'),
            'output': output if succeeded else contract.get('last_error'),
            'contract_id': cid,
            'shade_id': contract.get('shade_agent_id'),
        }
        if promote:
            result.update(_maybe_promote_output(
                ctx.state_dir, objective=objective, output=output if succeeded else None,
                shade_id=contract.get('shade_agent_id'), contract_id=cid,
            ))
        return result

    mod.spawn_shade = spawn_shade
    mod.rlm = rlm
    return mod


def _split_last_expr(code: str):
    """Split code into (exec_part, eval_part). eval_part is an ast.Expression
    for a trailing bare expression (REPL-style value capture), else None.
    Falls back to running the whole snippet as-is on a parse error so the
    real SyntaxError still surfaces from `exec`.
    """
    try:
        tree = ast.parse(code, mode='exec')
    except SyntaxError:
        return code, None
    if not tree.body or not isinstance(tree.body[-1], ast.Expr):
        return code, None
    exec_tree = ast.Module(body=tree.body[:-1], type_ignores=getattr(tree, 'type_ignores', []))
    eval_tree = ast.Expression(body=tree.body[-1].value)
    ast.fix_missing_locations(exec_tree)
    ast.fix_missing_locations(eval_tree)
    return exec_tree, eval_tree


def _run_one(namespace: dict, code: str) -> dict:
    out_buf, err_buf = io.StringIO(), io.StringIO()
    result_repr = None
    error = None
    tb = None
    try:
        exec_part, eval_part = _split_last_expr(code)
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            exec(compile(exec_part, '<kernel>', 'exec'), namespace)
            if eval_part is not None:
                value = eval(compile(eval_part, '<kernel>', 'eval'), namespace)
                if value is not None:
                    namespace['_'] = value
                    result_repr = repr(value)
    except KeyboardInterrupt:
        # The parent's timeout handling sends SIGINT rather than killing this
        # process outright, so a long-running call can be cut short without
        # losing the namespace. `with` blocks above already ran their
        # __exit__ by the time we get here, so out_buf/err_buf keep whatever
        # was printed before the interrupt, and any variable assignment that
        # completed before the interrupt point is already in `namespace`.
        error = 'KeyboardInterrupt: interrupted (timeout)'
        tb = traceback.format_exc()
    except Exception:
        error = ''.join(traceback.format_exception_only(*sys.exc_info()[:2])).strip()
        tb = traceback.format_exc()

    response: dict = {
        'ok': error is None,
        'stdout': out_buf.getvalue(),
        'stderr': err_buf.getvalue(),
    }
    if result_repr is not None:
        response['result'] = result_repr
    if error is not None:
        response['error'] = error
        response['traceback'] = tb
    return response


def main() -> None:
    namespace: dict = {'__name__': '__charon_kernel__'}
    init_args: dict = {}

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            print(json.dumps({'ok': False, 'error': f'bad request: {e}'}), flush=True)
            continue

        cmd = msg.get('cmd')

        if cmd == 'init':
            init_args = msg
            src_dir = str(msg.get('src_dir') or '')
            if src_dir and src_dir not in sys.path:
                sys.path.insert(0, src_dir)
            try:
                namespace['charon'] = _bridge_module(
                    str(msg.get('project_root', '')), str(msg.get('state_dir', '')), str(msg.get('agent_id', '')),
                    topology_depth=int(msg.get('topology_depth') or 0),
                    topology_budget=msg.get('topology_budget') if isinstance(msg.get('topology_budget'), dict) else None,
                )
                print(json.dumps({'ok': True}), flush=True)
            except Exception as e:
                print(json.dumps({'ok': False, 'error': str(e)}), flush=True)
            continue

        if cmd == 'shutdown':
            print(json.dumps({'ok': True}), flush=True)
            return

        if cmd == 'reset':
            namespace = {'__name__': '__charon_kernel__'}
            if init_args:
                try:
                    namespace['charon'] = _bridge_module(
                        str(init_args.get('project_root', '')),
                        str(init_args.get('state_dir', '')),
                        str(init_args.get('agent_id', '')),
                        topology_depth=int(init_args.get('topology_depth') or 0),
                        topology_budget=init_args.get('topology_budget') if isinstance(init_args.get('topology_budget'), dict) else None,
                    )
                except Exception:
                    pass
            print(json.dumps({'ok': True}), flush=True)
            continue

        if cmd != 'run':
            print(json.dumps({'ok': False, 'error': f'unknown cmd: {cmd}'}), flush=True)
            continue

        charon_mod = namespace.get('charon')
        call_state = getattr(charon_mod, '_call_state', None)
        if call_state is not None:
            timeout_sec = msg.get('timeout_sec')
            call_state['deadline'] = (time.time() + float(timeout_sec)) if timeout_sec else None

        response = _run_one(namespace, str(msg.get('code', '')))
        print(json.dumps(response), flush=True)


if __name__ == '__main__':
    main()
