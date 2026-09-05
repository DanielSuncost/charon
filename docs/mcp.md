# Charon tools over MCP

`charon.mcp_server` serves Charon's tools to any MCP client (Claude Code, Codex, …) over the
stdio transport: one JSON-RPC 2.0 message per line on stdin/stdout, logs on stderr.

```
python -m charon.mcp_server --project-root <dir> [--state-dir <dir>] [--workspace-root <dir>]
                            [--agent-id AG-…] [--profile all|overseer] [--tools A,B] [--approve none|all]
```

| flag | meaning |
|---|---|
| `--project-root` | directory the tools operate on (`ToolContext.project_root`; default cwd) |
| `--state-dir` | Charon state dir (`.charon_state`); default `$CHARON_STATE_DIR` |
| `--workspace-root` | workspace records root handed to the overseer tools via `ctx.metadata['workspace_root']` |
| `--agent-id` | actor id used for the tool context and approval session (default `AG-MCP`) |
| `--profile all` | every built-in + dynamic tool under its Charon name (`Read`, `Bash`, `FleetSend`, …) |
| `--profile overseer` | exactly the 20 tools of `docs/contracts/overseer-tools.json` under their contract names (`acheron_*`), routed to the `charon_name` executors; a missing executor returns an `isError` result, never a protocol error |
| `--tools A,B` | further restrict the exposed names |
| `--approve none\|all` | approval prompts are auto-denied (default; the call returns `Blocked: …` as `isError`) or auto-approved — there is no human on the other end of stdio |

Methods: `initialize` (echoes the client's `protocolVersion`, default `2025-03-26`), `notifications/*`
(no reply), `ping`, `tools/list`, `tools/call`. Errors: unknown method `-32601`, unknown tool `-32602`,
invalid JSON `-32700` (id `null`), batch `-32600`. Tool exceptions become `isError` results.

## Point Claude Code at it

`.mcp.json` (or `claude --mcp-config file.json --strict-mcp-config`):

```json
{
  "mcpServers": {
    "charon": {
      "command": "/path/to/charon/.venv/bin/python",
      "args": ["-m", "charon.mcp_server",
               "--project-root", "/path/to/project",
               "--profile", "overseer",
               "--workspace-root", "/path/to/project/.charon_state/projects/<slug>/workspace"],
      "env": { "PYTHONPATH": "/path/to/charon/src" }
    }
  }
}
```

With `--profile overseer` the tool names are the ones the overseer's role document already uses
(`acheron_fleet`, `acheron_dispatch`, …), so the same `CLAUDE.md` works whether Acheron or Charon
serves the tools. Allow them without prompts in the client with `mcp__charon` in
`.claude/settings.json` permissions.

Set `CHARON_OVERSEER_CONTRACT=/path/to/overseer-tools.json` to serve a different contract file.

Tests: `./.venv/bin/python -m pytest tests/test_mcp_server.py -q`.
