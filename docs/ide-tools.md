# Headless IDE tools

Charon exposes semantic coding services to the agent runtime, independently of
the terminal UI. This first increment stays in Python: no Rust runtime migration
or new mandatory Python dependency is required.

Discover tools with `ToolCatalog` (`query: "Lsp Ast Debug"`) and enable `Lsp`,
`Ast`, and/or `Debug`. They are deliberately outside the compact default set.
A running backend must restart after updating Charon to load the new registry.

## Trust and configuration

Language servers and debugger adapters are **trusted local programs**, not
sandboxes. Some language servers load plugins or invoke project build scripts.
LSP query calls therefore use Charon's dangerous-operation approval gate even
though their exposed protocol operations are read-only. Debugger launch and
execution control also require that gate. The global approval-off setting
remains an explicit operator override.

The optional operator file is `<state_dir>/ide.json`. It is **not** auto-loaded
from the repository root. A hosting runtime may instead supply these keys in
`ToolContext.metadata`; supplied metadata takes precedence. Do not accept
unreviewed executable configuration from a repository or model-generated file.

Example (replace paths with installed trusted executables):

```json
{
  "ide_lsp_servers": [
    {
      "name": "pyright",
      "command": ["/absolute/path/to/pyright-langserver", "--stdio"],
      "extensions": [".py", ".pyi"]
    }
  ],
  "ide_debug_adapters": {
    "python-demo": {
      "command": ["/absolute/path/to/python", "-m", "debugpy.adapter"],
      "launch": {
        "name": "Demo",
        "type": "python",
        "request": "launch",
        "program": "/absolute/path/to/project/demo.py",
        "cwd": "/absolute/path/to/project",
        "console": "internalConsole"
      }
    }
  }
}
```

Nothing installs language servers, ast-grep, or debugger adapters automatically.
Missing dependencies and invalid configuration are returned as errors, not
silently substituted with text matching or a claim of successful verification.

## Lsp

The initial surface is read-only: status, definition, references, hover,
symbols, diagnostics, and shutdown. It does not expose raw requests,
`executeCommand`, rename, or server-initiated workspace edits.

Without explicit server configuration, the service can discover installed
Python, TypeScript, and Rust servers. Use `status` to inspect availability.
Explicit file paths are constrained to regular files within the project root;
symlink escapes are rejected. Input line/column positions are one-based.

A language-service manager reuses servers by resolved project and configuration
within the **hosting Python process**. It is not a machine-wide daemon; separate
worker processes have separate managers. Document changes are reconciled before
queries. Successful Charon `Write` and `Edit` calls invalidate tracked LSP state
without starting a server. AST application uses `Edit`, so follows the same hook.

After editing, explicitly request `diagnostics`. An absent, stale, timed-out, or
unsupported diagnostic response is **not** clean verification. There is no
unconditional post-write server startup or slow diagnostics wait in every edit.
Shell/external changes are detected when the next query reconciles the file.

## Ast

Uses a validated installed `ast-grep` (or an `sg` executable whose version output
identifies it as ast-grep). It runs structural matching on a bounded UTF-8
snapshot passed through stdin; it never invokes ast-grep's mutation flags.
No directory traversal or project-provided rule/config loading is exposed.

```json
{"action":"search","path":"src/example.py","language":"python","pattern":"print($A)"}
```

For a rewrite, first request a preview:

```json
{"action":"preview","path":"src/example.py","language":"python","pattern":"print($A)","replacement":"logger.info($A)"}
```

Apply using the returned `previewToken` and the same parameters:

```json
{"action":"apply","path":"src/example.py","language":"python","pattern":"print($A)","replacement":"logger.info($A)","previewToken":"<returned token>"}
```

The token binds the preview to the actor/project, file contents, and requested
rewrite. It is not an approval and does not survive a runtime restart. Apply
rechecks freshness, frozen paths, and shade scope, then calls the normal `Edit`
dispatch with a file version. Only a single file can be rewritten per call;
there is no implied cross-file transaction. Search accepts bounded explicit
file lists. Replacement supports named captures, not arbitrary executable
transforms.

## Debug

A minimal Debug Adapter Protocol client launches only an operator-configured
adapter/launch pair. The model selects a configured name, not arbitrary adapter
argv or launch configuration. Debug sessions belong to one project and agent;
other agents cannot access them by guessing an ID.

Supported operations cover status, launch, breakpoints, continue, next, pause,
threads, stack, scopes, variables, and terminate. There is no arbitrary evaluate,
raw request, attach, or memory-write surface in this increment.

**Debug is unavailable under shade scope or frozen-path restrictions.** A
running debuggee can write anywhere its host process permits, so file-level
restrictions cannot honestly be enforced by the debugger interface. The parent
agent can own a debug session after approval; scoped implementation shades can
still work on code and use AST/LSP tools under their respective policies.

Use terminate to release owned sessions. Clients bound protocol messages,
request waits, and captured output, and report adapter exits and cancellation.
Live debugger and LSP process state is not persisted across Charon restarts.

## Boundaries and follow-up work

This is not a full graphical IDE and does not replace tests or compilers.
Deferred: semantic rename, code-action application, multi-file transactional
refactors, a machine-wide shared language-server broker, automatic diagnostics
in every edit result, richer debugger configuration/UI, and native acceleration.

Protocol fixtures exercise lifecycle, freshness, ownership, and failure handling
without requiring external servers. Tests using fixtures are not evidence of
compatibility with every real server/adapter; real-tool smoke tests should be
reported separately.
