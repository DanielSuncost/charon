"""Charon tools over MCP (stdio).

``python -m charon.mcp_server --project-root <dir> [--state-dir <dir>] [--workspace-root <dir>]
[--agent-id AG-…] [--profile overseer|all] [--tools A,B] [--approve none|all]``

Transport: MCP stdio — one JSON-RPC 2.0 message per line on stdin/stdout (UTF-8, no
framing headers). Everything diagnostic goes to stderr so stdout stays a clean
message stream. Any Claude Code / Codex session can point at this and gain Charon's
tools; the ``overseer`` profile exposes the 20 overseer-contract tools under their
contract names (``acheron_*``) so the same overseer role document works whether the
tools are served by Acheron or by Charon.

Methods: ``initialize``, ``notifications/initialized`` (no reply), ``ping``,
``tools/list``, ``tools/call``. Unknown method → -32601, unknown tool → -32602,
invalid JSON → -32700 (id null), batch → -32600. Tool failures never crash the server:
they come back as ``isError`` results.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from charon.tools import ToolContext, ToolResult, execute_tool, set_approval_callback, respond_to_approval

PROTOCOL_VERSION_DEFAULT = '2025-03-26'
SERVER_NAME = 'charon'
OVERSEER_CONTRACT_ENV = 'CHARON_OVERSEER_CONTRACT'

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL = -32603


# ── helpers ─────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    try:
        sys.stderr.write(f'[charon-mcp] {msg}\n')
        sys.stderr.flush()
    except Exception:
        pass


def charon_version() -> str:
    """Version from pyproject.toml when available (source checkout), else a constant."""
    try:
        root = Path(__file__).resolve().parents[2]
        pyproject = root / 'pyproject.toml'
        if pyproject.exists():
            for line in pyproject.read_text(encoding='utf-8').splitlines():
                s = line.strip()
                if s.startswith('version') and '=' in s:
                    return s.split('=', 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return '0.2.0'


def overseer_contract_path() -> Path:
    override = os.environ.get(OVERSEER_CONTRACT_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / 'docs' / 'contracts' / 'overseer-tools.json'


def load_overseer_contract(path: Path | None = None) -> list[dict[str, Any]]:
    """The overseer tool table: [{name, description, inputSchema, charon_name}, …]."""
    p = path or overseer_contract_path()
    data = json.loads(p.read_text(encoding='utf-8'))
    tools = data.get('tools') if isinstance(data, dict) else data
    if not isinstance(tools, list):
        raise ValueError(f'{p}: no tools list')
    return tools


def tool_def_to_mcp(tool_def: dict[str, Any]) -> dict[str, Any]:
    """Charon TOOL_DEF (input_schema) → MCP tool descriptor (inputSchema)."""
    schema = tool_def.get('inputSchema') or tool_def.get('input_schema') or {'type': 'object', 'properties': {}}
    return {
        'name': str(tool_def.get('name') or ''),
        'description': str(tool_def.get('description') or ''),
        'inputSchema': schema,
    }


def tool_result_to_mcp(result: ToolResult | Any) -> dict[str, Any]:
    if isinstance(result, ToolResult):
        text = result.content
        is_error = bool(result.is_error)
    elif isinstance(result, dict) and 'content' in result:
        text = str(result.get('content', ''))
        is_error = bool(result.get('is_error', False))
    else:
        text = '' if result is None else str(result)
        is_error = False
    return {'content': [{'type': 'text', 'text': str(text)}], 'isError': is_error}


# ── tool table ──────────────────────────────────────────────────────

class ToolTable:
    """What this server exposes: MCP name → (descriptor, resolve-executor-name)."""

    def __init__(self, profile: str, restrict: list[str] | None, state_dir: Path | None, project_root: Path):
        self.profile = profile
        self.restrict = [t.strip() for t in (restrict or []) if t.strip()]
        self.state_dir = state_dir
        self.project_root = project_root
        self._descriptors: list[dict[str, Any]] = []
        self._route: dict[str, str] = {}   # exposed name → Charon executor name
        self._build()

    def _build(self) -> None:
        if self.profile == 'overseer':
            for t in load_overseer_contract():
                exposed = str(t['name'])
                self._descriptors.append(tool_def_to_mcp(t))
                self._route[exposed] = str(t.get('charon_name') or exposed)
        else:
            defs = self._all_charon_defs()
            for d in defs:
                exposed = str(d.get('name') or '')
                if not exposed:
                    continue
                self._descriptors.append(tool_def_to_mcp(d))
                self._route[exposed] = exposed
        if self.restrict:
            allowed = set(self.restrict)
            self._descriptors = [d for d in self._descriptors if d['name'] in allowed]
            self._route = {k: v for k, v in self._route.items() if k in allowed}

    def _all_charon_defs(self) -> list[dict[str, Any]]:
        try:
            from charon.tools.dynamic_loader import get_all_tool_defs
            return list(get_all_tool_defs(self.state_dir, self.project_root))
        except Exception as exc:  # dynamic loading is best-effort
            _log(f'dynamic tools unavailable: {exc}')
            from charon.tools import ALL_TOOL_DEFS
            return list(ALL_TOOL_DEFS)

    def descriptors(self) -> list[dict[str, Any]]:
        return list(self._descriptors)

    def executor_name(self, exposed: str) -> str | None:
        return self._route.get(exposed)


# ── server ──────────────────────────────────────────────────────────

class McpServer:
    def __init__(self, *, project_root: Path, state_dir: Path | None, workspace_root: Path | None,
                 agent_id: str, profile: str, tools: list[str] | None, approve: str,
                 executor: Callable[[str, dict, ToolContext], Any] | None = None):
        self.project_root = project_root
        self.state_dir = state_dir
        self.workspace_root = workspace_root
        self.agent_id = agent_id or 'AG-MCP'
        self.profile = profile
        self.approve = approve
        self.table = ToolTable(profile, tools, state_dir, project_root)
        self._execute = executor or execute_tool
        self.protocol_version = PROTOCOL_VERSION_DEFAULT
        self._install_approval_policy()

    # approval: never block on a prompt nobody can answer
    def _install_approval_policy(self) -> None:
        decision = self.approve == 'all'

        def _cb(event: dict[str, Any]) -> None:
            if event.get('type') == 'approval_request':
                approval_id = str(event.get('approval_id') or '')
                if approval_id:
                    respond_to_approval(approval_id, decision)

        set_approval_callback(_cb)

    def context(self) -> ToolContext:
        return ToolContext(
            project_root=self.project_root,
            agent_id=self.agent_id,
            state_dir=self.state_dir,
            metadata={
                'workspace_root': str(self.workspace_root) if self.workspace_root else None,
                'surface': 'mcp',
                'profile': self.profile,
            },
        )

    # ── JSON-RPC ──
    def handle_line(self, line: str) -> dict[str, Any] | None:
        """One request line → one response dict, or None for notifications."""
        line = line.strip()
        if not line:
            return None
        try:
            msg = json.loads(line)
        except Exception as exc:
            return _error(None, JSONRPC_PARSE_ERROR, f'parse error: {exc}')
        if isinstance(msg, list):
            return _error(None, JSONRPC_INVALID_REQUEST, 'batch requests are not supported')
        if not isinstance(msg, dict):
            return _error(None, JSONRPC_INVALID_REQUEST, 'invalid request')
        return self.handle_message(msg)

    def handle_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        msg_id = msg.get('id')
        method = msg.get('method')
        params = msg.get('params') or {}
        is_notification = 'id' not in msg
        if not isinstance(method, str):
            return None if is_notification else _error(msg_id, JSONRPC_INVALID_REQUEST, 'missing method')
        if not isinstance(params, dict):
            return None if is_notification else _error(msg_id, JSONRPC_INVALID_PARAMS, 'params must be an object')
        try:
            if method == 'initialize':
                result = self._initialize(params)
            elif method == 'notifications/initialized':
                return None
            elif method.startswith('notifications/'):
                return None
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': self.table.descriptors()}
            elif method == 'tools/call':
                return self._tools_call(msg_id, params, is_notification)
            else:
                return None if is_notification else _error(msg_id, JSONRPC_METHOD_NOT_FOUND, f'method not found: {method}')
        except Exception as exc:  # never crash the loop
            _log(f'{method} failed: {exc}')
            return None if is_notification else _error(msg_id, JSONRPC_INTERNAL, str(exc))
        return None if is_notification else _ok(msg_id, result)

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get('protocolVersion')
        if isinstance(requested, str) and requested.strip():
            self.protocol_version = requested.strip()
        return {
            'protocolVersion': self.protocol_version,
            'capabilities': {'tools': {}},
            'serverInfo': {'name': SERVER_NAME, 'version': charon_version()},
        }

    def _tools_call(self, msg_id: Any, params: dict[str, Any], is_notification: bool) -> dict[str, Any] | None:
        name = params.get('name')
        arguments = params.get('arguments') or {}
        if not isinstance(name, str) or not name:
            return None if is_notification else _error(msg_id, JSONRPC_INVALID_PARAMS, 'tools/call needs a tool name')
        if not isinstance(arguments, dict):
            return None if is_notification else _error(msg_id, JSONRPC_INVALID_PARAMS, 'arguments must be an object')
        exec_name = self.table.executor_name(name)
        if exec_name is None:
            return None if is_notification else _error(msg_id, JSONRPC_INVALID_PARAMS, f'unknown tool: {name}')
        result = self.call_tool(exec_name, arguments)
        return None if is_notification else _ok(msg_id, result)

    def call_tool(self, exec_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool through Charon's execute_tool (approval, scope, built-in, dynamic)."""
        if self.profile == 'overseer' and not _executor_available(exec_name):
            return tool_result_to_mcp(ToolResult(
                content=f'tool not available on this Charon: {exec_name} (charon.tools.overseer_tool is not installed)',
                is_error=True,
            ))
        try:
            return tool_result_to_mcp(self._execute(exec_name, arguments, self.context()))
        except Exception as exc:
            return tool_result_to_mcp(ToolResult(content=f'tool error: {exc}', is_error=True))

    # ── stdio loop ──
    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        _log(f'serving profile={self.profile} tools={len(self.table.descriptors())} project_root={self.project_root}')
        for line in stdin:
            resp = self.handle_line(line)
            if resp is None:
                continue
            stdout.write(json.dumps(resp, ensure_ascii=False) + '\n')
            stdout.flush()


def _executor_available(exec_name: str) -> bool:
    from charon.tools import TOOL_EXECUTORS
    if exec_name in TOOL_EXECUTORS:
        return True
    try:
        from charon.tools.dynamic_loader import _dynamic_tools
        return exec_name in _dynamic_tools
    except Exception:
        return False


def _ok(msg_id: Any, result: Any) -> dict[str, Any]:
    return {'jsonrpc': '2.0', 'id': msg_id, 'result': result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {'jsonrpc': '2.0', 'id': msg_id, 'error': {'code': code, 'message': message}}


# ── CLI ─────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='charon.mcp_server', description='Serve Charon tools over MCP (stdio).')
    p.add_argument('--project-root', default='.', help='project directory tools operate on (default: cwd)')
    p.add_argument('--state-dir', default=os.environ.get('CHARON_STATE_DIR') or None, help='Charon state dir (.charon_state)')
    p.add_argument('--workspace-root', default=None, help='workspace records root for overseer tools')
    p.add_argument('--agent-id', default='AG-MCP', help='actor id for tool context / approvals')
    p.add_argument('--profile', choices=['all', 'overseer'], default='all', help='which tools to expose')
    p.add_argument('--tools', default=None, help='comma-separated allowlist of exposed tool names')
    p.add_argument('--approve', choices=['none', 'all'], default='none', help='auto-answer approval prompts (default: deny)')
    return p


def server_from_args(argv: list[str] | None = None) -> McpServer:
    args = build_parser().parse_args(argv)
    return McpServer(
        project_root=Path(args.project_root).expanduser().resolve(),
        state_dir=Path(args.state_dir).expanduser().resolve() if args.state_dir else None,
        workspace_root=Path(args.workspace_root).expanduser().resolve() if args.workspace_root else None,
        agent_id=args.agent_id,
        profile=args.profile,
        tools=[t for t in (args.tools or '').split(',') if t.strip()] or None,
        approve=args.approve,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass
    server = server_from_args(argv)
    try:
        server.serve()
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
