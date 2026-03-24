# Tmux-Native Architecture Plan

## Current State
- Charon TUI runs directly in user's terminal (bun process + python subprocess)
- Each `charon` invocation is an isolated instance
- No shared state between instances
- Conversation engine is in-process (dies with the TUI)

## Target Architecture

### Layer 1: Charon Daemon (background process)
A persistent Python process that:
- Manages all conversation engines (one per session)
- Manages shade orchestration
- Persists state to SQLite
- Listens on a Unix socket for TUI connections
- Runs inside a hidden tmux session (`charon-daemon`)
- Auto-starts on first `charon` invocation, persists after exit

### Layer 2: Tmux Session Per Agent
Each active Charon agent session runs in its own tmux session:
- `charon-main` — primary user session
- `charon-shade-001` — shade worker
- `charon-shade-002` — another shade

The TUI renders into the tmux session. Multiple terminals can `tmux attach -t charon-main` to see the same session.

### Layer 3: TUI Frontend (thin client)
The `charon` command becomes a thin client that:
1. Starts daemon if not running
2. Creates or attaches to a tmux session
3. Renders the OpenTUI interface inside tmux
4. Communicates with daemon via Unix socket

### Flow: `charon` command
```
$ charon
  1. Check if charon-daemon tmux session exists
     → No: start daemon in detached tmux
  2. Check if charon-main tmux session exists
     → No: create it, run TUI inside it
     → Yes: attach to it (multi-terminal view)
  3. TUI connects to daemon via /tmp/charon-daemon.sock
  4. All conversation goes through daemon
  5. On Ctrl+C: detach from tmux (daemon + session persist)
  6. On `charon` again: re-attach to existing session
```

### Flow: Session Grid
Since every agent runs in tmux:
- Session grid captures ALL agent tmux sessions
- Live updates work automatically (tmux capture)
- Enter on a session = `tmux switch-client` or split view
- Changes in one window appear in all attached terminals

### Implementation Phases

#### Phase 1: Daemon (foundation)
- [ ] `charon-daemon.py` — persistent background process
- [ ] Unix socket listener (JSON-RPC or simple line protocol)
- [ ] Conversation engine pool (one per session)
- [ ] Auto-start/stop lifecycle
- [ ] PID file at `/tmp/charon-daemon.pid`

#### Phase 2: TUI as tmux client
- [ ] `charon` script checks for daemon, starts if needed
- [ ] Creates tmux session if needed, attaches if exists
- [ ] TUI communicates with daemon instead of spawning python subprocess
- [ ] Detach on exit instead of killing everything

#### Phase 3: Multi-terminal sync
- [ ] Daemon broadcasts state changes to all connected TUIs
- [ ] Session grid pulls live tmux captures from daemon
- [ ] Agent status updates propagate to all viewers

#### Phase 4: Shade tmux sessions
- [ ] SpawnShade creates a tmux session for the shade
- [ ] Shade's conversation engine runs inside its tmux session
- [ ] Session grid shows shade activity in real-time
- [ ] Parent Charon can watch shade work via session grid

## Alternative: Simpler Approach
Skip the daemon and just use tmux directly:
- `charon` always runs inside tmux
- Multiple terminals attach to same tmux session
- Conversation state saved to disk (already done)
- No daemon needed — tmux IS the persistence layer
- Limitation: can't have independent backend state

## Decision
Start with the simpler approach (tmux-only) for immediate multi-terminal,
then add daemon for independent backend state and shade orchestration.
