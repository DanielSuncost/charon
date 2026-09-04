"""Charon persistent Python kernel worker — runs as a standalone child process.

Launched by pykernel_tool.py via `sys.executable <this file>`. Speaks a tiny
line-delimited JSON protocol over stdin/stdout:

    in:  {"cmd": "init", "project_root": "...", "state_dir": "...",
          "agent_id": "...", "src_dir": "..."}
    out: {"ok": true}

    in:  {"cmd": "run", "code": "..."}
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


def _bridge_module(project_root: str, state_dir: str, agent_id: str) -> types.ModuleType:
    """Build the `charon` object exposed inside kernel globals.

    Deliberately thin: spawn_shade and rlm both re-enter Charon's own
    SpawnShade tool function rather than reimplementing shade-spawning, so
    kernel-issued spawns and normal tool-call spawns stay behind one
    implementation. rlm() additionally polls shade_orchestrator.get_contract
    (read-only) to turn that fire-and-forget spawn into a blocking call —
    see rlm()'s own docstring for why polling, not a synchronous
    reimplementation of the shade's phase loop, is the safe way to do that.
    """
    mod = types.ModuleType('charon')

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

    def rlm(objective: str, *, child_agent_id=None, scope=None, constraints=None,
            expected_outputs=None, contract_id=None, retain: bool = False,
            promote: bool = False, task_complexity: str = 'normal',
            poll_interval: float = 0.5) -> dict:
        """Call a sub-agent and block for its result, like a function call.

        Unlike spawn_shade (fire-and-forget), this waits for the child to
        finish and returns its actual output — the real recursive-call
        primitive: call a sub-agent the way you'd call a function.

        PyKernel clamps a single `run` call to at most 300s, so a child
        whose work takes longer WILL make this call hit that ceiling and
        get cut short by a soft interrupt before it can return. Because of
        that, the contract id is printed to stdout immediately after the
        child is spawned, before the wait begins — a raised interrupt loses
        this function's local variables, but not what was already printed.
        Read that line and pass it back in via contract_id= on a follow-up
        rlm() call to keep waiting on the same child, instead of spawning a
        duplicate.

        child_agent_id: call an existing retained (idle) shade instead of
        spawning a fresh one. Errors — never silently spawns a fresh shade
        under that id — if it isn't found, isn't a shade, or isn't idle.

        retain: only meaningful on a fresh spawn (child_agent_id omitted).
        If true, the new shade goes idle instead of stopping once this call
        returns, and can be called again later via
        rlm(objective, child_agent_id=<the id from this call's result>).

        promote: if the call completes successfully, have an independent
        LLM judge score whether the output is a genuine, reusable finding
        worth remembering for this project (not routine or trivial) before
        writing it into durable project memory — never on the shade's own
        say-so. See the result's 'promoted'/'promotion_score' keys.

        task_complexity: 'simple'/'normal'/'complex' — only meaningful on a
        fresh spawn; 'complex' asks for the strong model tier. Downgraded
        automatically once this tree's token_budget is mostly spent,
        regardless of what's requested here.
        """
        import time as _time
        from pathlib import Path

        from charon.tools import ToolContext
        from charon.tools.shade_tool import execute_spawn_shade
        from charon.shade.shade_orchestrator import get_contract

        ctx = ToolContext(
            project_root=Path(project_root),
            agent_id=agent_id,
            state_dir=Path(state_dir) if state_dir else None,
        )

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
            _time.sleep(poll_interval)

        phases = contract.get('phases') or []
        succeeded = contract.get('status') == 'completed'
        output = phases[-1].get('result_summary') if phases else None
        meta = contract.get('metadata') or {}
        _trace_rlm_node(
            ctx.state_dir, node_id=cid, parent_id=agent_id, root_task_id=agent_id,
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
                    )
                except Exception:
                    pass
            print(json.dumps({'ok': True}), flush=True)
            continue

        if cmd != 'run':
            print(json.dumps({'ok': False, 'error': f'unknown cmd: {cmd}'}), flush=True)
            continue

        response = _run_one(namespace, str(msg.get('code', '')))
        print(json.dumps(response), flush=True)


if __name__ == '__main__':
    main()
