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


def _bridge_module(project_root: str, state_dir: str, agent_id: str) -> types.ModuleType:
    """Build the `charon` object exposed inside kernel globals.

    Deliberately thin: spawn_shade re-enters Charon's own SpawnShade tool
    function rather than reimplementing shade-spawning, so kernel-issued
    spawns and normal tool-call spawns stay behind one implementation.
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

    mod.spawn_shade = spawn_shade
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
