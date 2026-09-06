"""PyKernel tool — a persistent Python REPL kernel, one per agent.

Unlike ExecuteCode (one-shot subprocess, no memory between calls), PyKernel
keeps a single long-lived child process alive for the agent's whole session:
variables, imports, and function definitions persist across tool calls, turns,
and context compaction — because none of that state lives in the conversation
transcript at all. This is Charon's answer to the "recursive language model"
idea of treating context as variables: hold large intermediate data (scraped
pages, dataframes, file trees) as kernel-resident Python objects instead of
paying context tokens to shuttle it through tool-call/tool-result round trips.

The kernel also exposes `charon.spawn_shade(...)` inside its namespace, which
re-enters the real SpawnShade tool and returns immediately with a handle —
fire-and-forget, matching how shade dispatch already works everywhere else.

For a real recursive call — spawn a sub-agent and block for its actual
result, like calling a function — use `charon.rlm(objective, ...)` instead.
It polls the child's contract from inside this same kernel call, so
PyKernel's existing timeout/soft-interrupt handling doubles as rlm()'s
timeout handling with no new machinery: a child that runs long enough to
hit the timeout just interrupts the wait (state preserved, same as any
other long-running kernel call), not the child itself.

`charon.rlm(objective, retain=True)` (or `SpawnShade`'s own `retain=True`)
keeps the new shade addressable after its work ends (idle, not stopped)
instead of self-terminating — the result's `shade_id` is how you reach it
again. Call it again later with
`charon.rlm(objective, child_agent_id=<its id>)` — it resumes with its full
prior conversation rather than starting fresh.

`charon.rlm(objective, promote=True)` has an independent LLM judge score
whether the result is a genuine, reusable finding — not just routine
progress — before writing it into durable project memory. Never promoted
on the shade's own say-so.

`charon.rlm(objective, peer_agent_id=<id>)` messages another already-running
agent (not a shade you spawned) and blocks for its reply, via the real task
queue rather than the write-only agent inbox — capped so two agents can't
message each other in an unbounded loop.

Not a sandbox. Code runs as a subprocess with the daemon's own OS permissions,
the same trust model as Bash and ExecuteCode.
"""
from __future__ import annotations

import atexit
import json
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from charon.tools import ToolContext, ToolResult, truncate_output

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


PYKERNEL_TOOL_DEF = {
    'name': 'PyKernel',
    'description': (
        'A persistent Python kernel, one per agent. Unlike ExecuteCode, variables, '
        'imports, and function definitions survive across calls — use it to hold large '
        'intermediate data (scraped pages, dataframes, file listings) as Python objects '
        'instead of re-reading them through tool results every turn, and for looped or '
        'stateful processing. Inside the kernel, charon.spawn_shade(goal, scope=[...]) '
        'spawns a real shade agent and returns immediately with a handle — it does not '
        'wait for the shade to finish. charon.rlm(objective, ...) is the blocking version: '
        'it spawns a shade and waits for its actual result, like a recursive function call. '
        'rlm() prints its contract_id before waiting — if a long-running rlm() call gets '
        'interrupted by this tool\'s own timeout, read that printed contract_id from stdout '
        'and pass it back in as contract_id= on a follow-up PyKernel call to keep waiting on '
        'the same child instead of spawning a duplicate. Not sandboxed: same trust level as Bash.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['run', 'reset', 'status'],
                'description': 'Default: run.',
            },
            'code': {'type': 'string', 'description': 'Python code to run in the persistent kernel.'},
            'timeout_sec': {'type': 'number', 'description': 'Execution timeout in seconds (default 60, max 300).'},
        },
        'required': ['code'],
    },
}


_WORKER_SCRIPT = str(Path(__file__).with_name('_pykernel_worker.py'))
_SRC_DIR = str(Path(__file__).resolve().parents[2])  # .../src — so `import charon` works in the child

_kernels_lock = threading.Lock()
_kernels: dict[str, '_KernelWorker'] = {}


class _KernelWorker:
    """One persistent child process, its background stdout reader, and the
    lock that serializes request/response pairs against it."""

    def __init__(
        self, project_root: Path, state_dir: Path | None, agent_id: str,
        topology_depth: int = 0, topology_budget: dict | None = None,
    ):
        self.agent_id = agent_id
        self.created_at = time.time()
        self.calls = 0
        self._io_lock = threading.Lock()
        self._proc = subprocess.Popen(
            [sys.executable, _WORKER_SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=str(project_root),
        )
        self._out_q: "queue.Queue[str]" = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # topology_depth/topology_budget: inherited once, at kernel creation,
        # from whatever ToolContext first spawned this kernel — an agent's
        # position in a delegation tree doesn't change over its lifetime, so
        # this isn't re-sent on later calls the way code/timeout_sec are.
        # Without this, a shade that itself calls PyKernel would have its own
        # kernel mint a fresh, disconnected tree instead of continuing its
        # parent's (see docs/plans root_task_id fragmentation fix).
        self._init_args = {
            'project_root': str(project_root),
            'state_dir': str(state_dir) if state_dir else '',
            'agent_id': agent_id,
            'src_dir': _SRC_DIR,
            'topology_depth': int(topology_depth or 0),
            'topology_budget': topology_budget if isinstance(topology_budget, dict) else None,
        }
        self._send({'cmd': 'init', **self._init_args})
        ack = self._recv(timeout=30)
        if not ack or not ack.get('ok'):
            _diag('pykernel_tool', 'kernel init handshake failed or timed out', agent_id=agent_id, ack=ack)

    def _read_loop(self) -> None:
        try:
            proc_stdout = self._proc.stdout
            if proc_stdout is None:
                return
            for line in proc_stdout:
                self._out_q.put(line)
        except Exception:
            pass

    def _send(self, msg: dict) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(msg) + '\n')
        self._proc.stdin.flush()

    def _recv(self, timeout: float) -> dict | None:
        try:
            line = self._out_q.get(timeout=timeout)
        except queue.Empty:
            return None
        try:
            return json.loads(line)
        except Exception:
            return {'ok': False, 'error': f'malformed kernel response: {line[:200]}'}

    def alive(self) -> bool:
        return self._proc.poll() is None

    def run(self, code: str, timeout_sec: float) -> dict:
        if not self.alive():
            return {
                'ok': False,
                'error': 'kernel process is not running (crashed or timed out previously); '
                         'use action="reset" to start a fresh one',
            }
        with self._io_lock:
            self.calls += 1
            try:
                self._send({'cmd': 'run', 'code': code, 'timeout_sec': timeout_sec})
            except Exception as e:
                return {'ok': False, 'error': f'failed to send code to kernel: {e}'}
            resp = self._recv(timeout=timeout_sec)
            if resp is not None:
                return resp

            # Timed out. Soft-interrupt first: SIGINT raises KeyboardInterrupt
            # inside the running exec, so whatever the namespace accumulated
            # before the timeout point survives — a timeout is not a reason to
            # destroy state the model may still need (harness friction should
            # not masquerade as a capability failure). This resolves promptly
            # for pure-Python loops and most blocking I/O (time.sleep included
            # — CPython delivers pending signals through it); it will not
            # interrupt a call blocked in a C extension with the GIL released
            # and no signal checks, which is what the hard-kill fallback below
            # is for.
            try:
                self._proc.send_signal(signal.SIGINT)
                interrupted = self._recv(timeout=min(10.0, max(2.0, timeout_sec * 0.2)))
            except Exception:
                interrupted = None
            if interrupted is not None:
                interrupted['ok'] = False
                interrupted.setdefault(
                    'error', f'interrupted after {timeout_sec}s (kernel state was preserved)',
                )
                return interrupted

            # Still unresponsive after a soft interrupt — genuinely stuck.
            # Kill it so the caller gets a clean signal rather than a silent
            # hang; state is lost, but only now, as a last resort.
            self.close()
            return {
                'ok': False,
                'error': f'kernel unresponsive after {timeout_sec}s and a soft interrupt; '
                         'it was stopped and state was lost',
            }

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass


def _get_or_create_kernel(ctx: ToolContext) -> _KernelWorker:
    key = ctx.agent_id or 'default'
    with _kernels_lock:
        existing = _kernels.get(key)
        if existing is not None and existing.alive():
            return existing
        worker = _KernelWorker(
            ctx.project_root, ctx.state_dir, key,
            topology_depth=getattr(ctx, 'topology_depth', 0) or 0,
            topology_budget=getattr(ctx, 'topology_budget', None),
        )
        _kernels[key] = worker
        return worker


def _reset_kernel(ctx: ToolContext) -> None:
    key = ctx.agent_id or 'default'
    with _kernels_lock:
        existing = _kernels.pop(key, None)
    if existing is not None:
        existing.close()


@atexit.register
def _shutdown_all_kernels() -> None:
    with _kernels_lock:
        workers = list(_kernels.values())
        _kernels.clear()
    for w in workers:
        try:
            w.close()
        except Exception:
            pass


def execute_pykernel(params: dict, ctx: ToolContext) -> ToolResult:
    action = str(params.get('action') or 'run').strip().lower()

    if action == 'reset':
        _reset_kernel(ctx)
        return ToolResult(content='Kernel reset. All variables and imports cleared.')

    if action == 'status':
        key = ctx.agent_id or 'default'
        with _kernels_lock:
            worker = _kernels.get(key)
        if worker is None:
            return ToolResult(content='No kernel running for this agent yet.', details={'alive': False})
        alive = worker.alive()
        return ToolResult(
            content=(
                f'Kernel {"running" if alive else "stopped"} — {worker.calls} call(s), '
                f'up {time.time() - worker.created_at:.0f}s.'
            ),
            details={'alive': alive, 'calls': worker.calls},
        )

    if action != 'run':
        return ToolResult(content=f'Unknown action: {action}', is_error=True)

    code = str(params.get('code') or '')
    if not code.strip():
        return ToolResult(content='Error: code is required.', is_error=True)

    timeout_sec = float(params.get('timeout_sec') or 60)
    timeout_sec = max(1.0, min(timeout_sec, 300.0))

    try:
        worker = _get_or_create_kernel(ctx)
    except Exception as e:
        _diag('pykernel_tool', 'kernel process failed to start', error=e, agent_id=ctx.agent_id)
        return ToolResult(content=f'Error starting kernel: {e}', is_error=True)

    resp = worker.run(code, timeout_sec)

    parts = []
    if resp.get('stdout'):
        parts.append(resp['stdout'])
    if resp.get('result') is not None:
        parts.append(f"Out: {resp['result']}")
    if resp.get('stderr'):
        parts.append('[stderr]\n' + resp['stderr'])

    if not resp.get('ok'):
        parts.append(f"Error: {resp.get('error', 'unknown kernel error')}")
        content = '\n'.join(parts) if parts else 'Error: unknown kernel error'
        content, truncated = truncate_output(content, max_lines=ctx.max_output_lines, max_bytes=ctx.max_output_bytes)
        return ToolResult(content=content, is_error=True, truncated=truncated)

    content = '\n'.join(parts).strip() or '(no output)'
    content, truncated = truncate_output(content, max_lines=ctx.max_output_lines, max_bytes=ctx.max_output_bytes)
    return ToolResult(content=content, truncated=truncated)
